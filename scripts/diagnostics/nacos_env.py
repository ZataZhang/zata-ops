"""List dotenv keys or inject Nacos text configuration without displaying values."""

from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from private_secrets import ExecutionOptions, run_with_environment
from zata_ops.private_credentials import SECRET_NAME, validate_private_directory


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication to a redirected endpoint.
        return None


def _request_json(request: Request) -> dict:
    with build_opener(_NoRedirect).open(request, timeout=15) as response:
        response_bytes = response.read(1024 * 1024 + 1)
    if len(response_bytes) > 1024 * 1024:
        raise ValueError("response too large")
    response_payload = json.loads(response_bytes)
    if not isinstance(response_payload, dict):
        raise ValueError("invalid response")
    return response_payload


def fetch_configuration(arguments: argparse.Namespace) -> str:
    """Fetch one configuration using credentials held only in local memory.

    Args:
        arguments: CLI endpoint, resource selectors and local credential references.

    Returns:
        Dotenv configuration text.

    Raises:
        ValueError: Invalid URL, credentials, response or configuration type.
        OSError: Network or local credential access fails.
    """
    endpoint = urlsplit(arguments.server)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/", "/nacos", "/nacos/"}
    ):
        raise ValueError("invalid server URL")
    validate_private_directory(arguments.private_dir)
    credentials = dotenv_values(
        arguments.private_dir / "secrets.env", interpolate=False, encoding="utf-8"
    )
    api_root = arguments.server.rstrip("/")
    if not api_root.endswith("/nacos"):
        api_root += "/nacos"
    if arguments.token_ref:
        access_token = credentials.get(arguments.token_ref)
    else:
        username = credentials.get(arguments.username_ref)
        password = credentials.get(arguments.password_ref)
        if not username or not password:
            raise ValueError("missing login credentials")
        login_response = _request_json(
            Request(
                api_root + "/v3/auth/user/login",
                data=urlencode({"username": username, "password": password}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        )
        access_token = login_response.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("missing access token")
    query_string = urlencode(
        {
            "namespaceId": arguments.namespace,
            "groupName": arguments.group,
            "dataId": arguments.data_id,
        }
    )
    config_response = _request_json(
        Request(
            api_root + "/v3/client/cs/config?" + query_string,
            headers={"accessToken": access_token},
        )
    )
    if config_response.get("code") != 0:
        raise ValueError("configuration request failed")
    config_payload = config_response.get("data")
    if (
        not isinstance(config_payload, dict)
        or config_payload.get("contentType", "").lower() != "text"
        or config_payload.get("encryptedDataKey")
        or config_payload.get("success") is False
        or not isinstance(config_payload.get("content"), str)
    ):
        raise ValueError("expected unencrypted text configuration")
    return config_payload["content"]


def parse_environment(configuration_text: str) -> dict[str, str | None]:
    """Parse dotenv strictly without expansion, logs or raw-value exceptions.

    Args:
        configuration_text: Nacos text content in dotenv syntax.

    Returns:
        Names mapped to literal values; None means no assignment.

    Raises:
        ValueError: Invalid syntax, duplicate or invalid name, or NUL value.
    """
    parsed_environment: dict[str, str | None] = {}
    for binding in parse_stream(StringIO(configuration_text)):
        if binding.error:
            raise ValueError("invalid dotenv syntax")
        if binding.key is None:
            continue
        if (
            not SECRET_NAME.fullmatch(binding.key)
            or binding.key in parsed_environment
            or (binding.value is not None and "\x00" in binding.value)
        ):
            raise ValueError("invalid dotenv entry")
        parsed_environment[binding.key] = binding.value
    return parsed_environment


def main() -> int:
    """Run the safe Nacos configuration CLI.

    Returns:
        Child exit code, zero for listing, or one on failure without raw errors.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Client API base URL")
    parser.add_argument("--namespace", default="public")
    parser.add_argument("--group", default="DEFAULT_GROUP")
    parser.add_argument("--data-id", required=True)
    parser.add_argument("--private-dir", type=Path, default=Path("config/private"))
    parser.add_argument("--token-ref", help="Local access-token credential name")
    parser.add_argument("--username-ref", default="NACOS_USERNAME")
    parser.add_argument("--password-ref", default="NACOS_PASSWORD")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("keys", help="List variable names only")
    run_parser = commands.add_parser(
        "run", help="Inject selected keys; hide child output"
    )
    run_parser.add_argument("--env", action="append", required=True, dest="names")
    run_parser.add_argument("--cwd", type=Path)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    try:
        configuration_text = fetch_configuration(arguments)
        parsed_environment = parse_environment(configuration_text)
        if arguments.action == "keys":
            print("\n".join(sorted(parsed_environment)))
            return 0
        selected_environment: dict[str, str] = {}
        for name in arguments.names:
            selected_value = parsed_environment.get(name)
            if selected_value is None:
                raise ValueError("missing or unassigned variable")
            selected_environment[name] = selected_value
        command = arguments.command
        if command[:1] == ["--"]:
            command = command[1:]
        return run_with_environment(
            selected_environment, command, ExecutionOptions(arguments.cwd)
        )
    except Exception:
        # HTTP errors and parser exceptions may contain credentials or response bodies.
        print(
            "Nacos operation failed; check credentials, resource and dotenv format. Values hidden.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Nacos operation cancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
