"""Exercise the real Nacos CLI against a local synthetic HTTP service."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts/diagnostics/nacos_env.py"


@pytest.fixture
def nacos_fixture(tmp_path):
    import tempfile

    private_root = REPO_ROOT / "config/private"
    private_root.mkdir(parents=True, exist_ok=True)
    response_state = {
        "content": 'TOKEN="synthetic-${LITERAL}-value"\nEMPTY=\nUNASSIGNED\nPORT=42\n'
    }
    request_records = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request_body = self.rfile.read(int(self.headers["Content-Length"]))
            request_records.append((self.path, request_body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accessToken":"synthetic-access-token"}')

        def do_GET(self):
            request_records.append((self.path, self.headers.get("accessToken")))
            if response_state.get("redirect"):
                self.send_response(302)
                self.send_header("Location", "/synthetic-redirect")
                self.end_headers()
                return
            if response_state.get("failure"):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"synthetic-sensitive-error")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "code": 0,
                        "data": {
                            "contentType": "text",
                            "content": response_state["content"],
                        },
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    with tempfile.TemporaryDirectory(
        prefix="test-nacos-", dir=private_root
    ) as directory:
        credential_directory = Path(directory)
        (credential_directory / "secrets.env").write_text(
            "NACOS_USERNAME=synthetic-user\nNACOS_PASSWORD=synthetic-password\nACCESS_TOKEN=synthetic-access-token\n",
            encoding="utf-8",
        )

        def invoke(arguments):
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--server",
                    f"http://127.0.0.1:{server.server_port}",
                    "--data-id",
                    "app.env",
                    "--private-dir",
                    directory,
                    *arguments,
                ],
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
            )

        yield invoke, response_state, request_records, tmp_path
    server.shutdown()
    server.server_close()
    server_thread.join()


def test_keys_login_and_token(nacos_fixture):
    invoke, _, request_records, _ = nacos_fixture
    listed = invoke(["keys"])
    assert listed.returncode == 0
    assert listed.stdout.splitlines() == ["EMPTY", "PORT", "TOKEN", "UNASSIGNED"]
    assert "synthetic" not in listed.stdout + listed.stderr
    assert request_records[0][0] == "/nacos/v3/auth/user/login"
    assert "password=synthetic-password" in request_records[0][1].decode()
    assert request_records[1][1] == "synthetic-access-token"
    assert "accessToken" not in request_records[1][0]
    request_records.clear()
    assert invoke(["--token-ref", "ACCESS_TOKEN", "keys"]).returncode == 0
    assert len(request_records) == 1


def test_injection_literal_empty_selection_output_and_exit(nacos_fixture):
    invoke, _, _, _ = nacos_fixture
    child_script = (
        "import os,sys; "
        "assert os.environ['TOKEN']=='synthetic-${LITERAL}-value'; "
        "assert os.environ['EMPTY']==''; assert 'PORT' not in os.environ; "
        "assert 'NACOS_PASSWORD' not in os.environ; "
        "print(os.environ['TOKEN']); print('child-error',file=sys.stderr); sys.exit(7)"
    )
    executed = invoke(
        [
            "run",
            "--env",
            "TOKEN",
            "--env",
            "EMPTY",
            "--",
            sys.executable,
            "-c",
            child_script,
        ]
    )
    assert executed.returncode == 7
    assert "exit=7" in executed.stdout
    assert "synthetic" not in executed.stdout + executed.stderr
    assert "child-error" not in executed.stdout + executed.stderr


@pytest.mark.parametrize(
    "content",
    [
        'TOKEN="unterminated-synthetic',
        "TOKEN=a\nTOKEN=b",
        "bad-name=synthetic",
        'TOKEN="null\x00synthetic"',
    ],
)
def test_invalid_config_never_echoes_values(nacos_fixture, content):
    invoke, response_state, _, _ = nacos_fixture
    response_state["content"] = content
    executed = invoke(["keys"])
    assert executed.returncode == 1
    assert "synthetic" not in executed.stdout + executed.stderr
    assert not executed.stdout


@pytest.mark.parametrize("name", ["MISSING", "UNASSIGNED"])
def test_missing_value_prevents_execution(nacos_fixture, name):
    invoke, _, _, temporary_directory = nacos_fixture
    marker_path = temporary_directory / "launched"
    executed = invoke(
        [
            "run",
            "--env",
            name,
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker_path)!r}).touch()",
        ]
    )
    assert executed.returncode == 1
    assert not marker_path.exists()


@pytest.mark.parametrize("failure_mode", ["redirect", "failure"])
def test_http_failures_are_hidden_and_redirects_not_followed(
    nacos_fixture, failure_mode
):
    invoke, response_state, request_records, _ = nacos_fixture
    response_state[failure_mode] = True
    executed = invoke(["--token-ref", "ACCESS_TOKEN", "keys"])
    assert executed.returncode == 1
    assert len(request_records) == 1
    assert "synthetic" not in executed.stdout + executed.stderr
