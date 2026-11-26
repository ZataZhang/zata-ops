"""Deliver OpenBao audit events to a versioned, immutable S3 archive."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import tomllib
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import typer
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

_MAX_EVENT = 1024 * 1024
_BATCH_SIZE = 50
_REQUEST_ID = re.compile(r"[0-9a-f-]{36}\Z")
_STREAM_PREFIX = re.compile(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*\Z")


class ArchiveError(ValueError):
    """A sanitized audit archive failure."""


class ArchiveConfig(BaseModel):
    """Local archive paths and independently managed S3 destination."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: str
    checkpoint: str
    bucket: str = Field(min_length=1)
    prefix: str
    retention_days: int = Field(ge=1)
    endpoint_url: str = ""
    ca_file: str = ""
    region: str = ""
    path_style: bool = False
    credential_mount: str = ""


class LockedArchive:
    """Persist events and verify retention before acknowledging them locally."""

    def __init__(self, settings: ArchiveConfig, directory: Path) -> None:
        """Build the AWS client with TLS verification and bounded retries."""
        if not _STREAM_PREFIX.fullmatch(settings.prefix):
            raise ArchiveError("invalid_archive_prefix")
        if settings.endpoint_url:
            endpoint = urlsplit(settings.endpoint_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path not in {"", "/"}
                or endpoint.query
                or endpoint.fragment
            ):
                raise ArchiveError("invalid_archive_endpoint")
        self.settings = settings
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url or None,
            region_name=settings.region or None,
            verify=str(directory / settings.ca_file) if settings.ca_file else True,
            config=Config(
                connect_timeout=5,
                read_timeout=10,
                ignore_configured_endpoint_urls=True,
                retries={"total_max_attempts": 2},
                s3={"addressing_style": "path" if settings.path_style else "auto"},
            ),
        )

    def store(self, event_line: bytes) -> dict[str, object] | None:
        """Confirm the exact event is stored in a protected object version."""
        try:
            event = json.loads(event_line)
            request_id = event["request"]["id"]
            event_type = event["type"]
            if (
                not isinstance(request_id, str)
                or not _REQUEST_ID.fullmatch(request_id)
                or event_type not in {"request", "response"}
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, RecursionError) as error:
            raise ArchiveError("invalid_audit_event") from error
        digest = hashlib.sha256(event_line)
        checksum = base64.b64encode(digest.digest()).decode("ascii")
        locator = {
            "Bucket": self.settings.bucket,
            "Key": f"{self.settings.prefix}/{request_id}/{event_type}-{digest.hexdigest()}.jsonl",
        }
        try:
            stored = self.client.put_object(
                **locator,
                Body=event_line,
                ContentType="application/x-ndjson",
                ChecksumSHA256=checksum,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            stored = self.client.head_object(**locator)
        version = stored.get("VersionId")
        if not version or version == "null":
            raise ArchiveError("archive_version_missing")
        pinned_locator = {**locator, "VersionId": version}
        confirmed = self.client.head_object(**pinned_locator, ChecksumMode="ENABLED")
        retain_until = confirmed.get("ObjectLockRetainUntilDate")
        last_modified = confirmed.get("LastModified")
        if (
            confirmed.get("VersionId") != version
            or confirmed.get("ObjectLockMode") != "COMPLIANCE"
            or retain_until is None
            or last_modified is None
            or retain_until
            < last_modified + timedelta(days=self.settings.retention_days)
        ):
            raise ArchiveError("archive_retention_unconfirmed")
        if confirmed.get("ChecksumSHA256") != checksum:
            with closing(
                self.client.get_object(**pinned_locator)["Body"]
            ) as archived_event:
                if archived_event.read(len(event_line) + 1) != event_line:
                    raise ArchiveError("archive_checksum_mismatch")
        return self._access_event(event, request_id)

    def _access_event(self, event: dict, request_id: str) -> dict[str, object] | None:
        mount = self.settings.credential_mount
        request = event["request"]
        path = request.get("path", "")
        if (
            not mount
            or event["type"] != "response"
            or request.get("operation") != "read"
            or not isinstance(path, str)
            or not path.startswith(f"{mount}/data/")
            or not _STREAM_PREFIX.fullmatch(path)
        ):
            return None
        auth = event.get("auth")
        actor = (
            (auth.get("entity_id") or auth.get("accessor"))
            if isinstance(auth, dict)
            else None
        )
        actor_is_safe = isinstance(actor, str) and re.fullmatch(
            r"(?:[0-9a-f-]{36}|hmac-sha256:[0-9a-f]{64})", actor
        )
        return {
            "event": "credential_read",
            "audit_request_id": request_id,
            "credential_path": path,
            "audit_actor": actor if actor_is_safe else "unattributed",
            "outcome": "error" if event.get("error") else "ok",
        }


def _archive_file(
    source_path: Path,
    archive: LockedArchive,
    database: sqlite3.Connection,
    access_events: list[dict[str, object]],
) -> tuple[int, int]:
    with source_path.open("rb") as source_file:
        file_info = os.fstat(source_file.fileno())
        first_event = source_file.readline(_MAX_EVENT + 1)
        if not first_event or not first_event.endswith(b"\n"):
            if len(first_event) > _MAX_EVENT:
                raise ArchiveError("audit_event_too_large")
            return 0, file_info.st_size
        stream = f"{file_info.st_dev}:{file_info.st_ino}:{hashlib.sha256(first_event).hexdigest()}"
        checkpoint = database.execute(
            "SELECT offset FROM streams WHERE stream = ?", (stream,)
        ).fetchone()
        offset = checkpoint[0] if checkpoint else 0
        if offset > file_info.st_size:
            raise ArchiveError("audit_source_truncated")
        source_file.seek(offset)
        archived_count = 0
        for _ in range(_BATCH_SIZE):
            event_line = source_file.readline(_MAX_EVENT + 1)
            if len(event_line) > _MAX_EVENT:
                raise ArchiveError("audit_event_too_large")
            if not event_line.endswith(b"\n"):
                break
            access_event = archive.store(event_line)
            if access_event is not None:
                access_events.append(access_event)
            offset = source_file.tell()
            database.execute(
                "INSERT INTO streams (stream, offset) VALUES (?, ?) "
                "ON CONFLICT(stream) DO UPDATE SET offset=excluded.offset",
                (stream, offset),
            )
            archived_count += 1
        return archived_count, max(0, os.fstat(source_file.fileno()).st_size - offset)


def archive_command(config: Path = typer.Option(..., "--config")) -> None:
    """Archive complete audit events and report whether local backlog remains."""
    try:
        settings = ArchiveConfig.model_validate(
            tomllib.loads(config.read_text(encoding="utf-8"))
        )
        archive = LockedArchive(settings, config.parent)
        source = config.parent / settings.source
        checkpoint = config.parent / settings.checkpoint
        if not source.is_file() or source.is_symlink() or checkpoint.is_symlink():
            raise ArchiveError("invalid_audit_source_or_checkpoint")
        destination = json.dumps(
            [
                settings.endpoint_url,
                settings.bucket,
                settings.prefix,
                settings.retention_days,
            ]
        )
        totals = {"archived_events": 0, "pending_bytes": 0}
        access_events: list[dict[str, object]] = []
        sources = sorted(
            candidate
            for candidate in source.parent.glob(source.name + ".*")
            if candidate.is_file()
            and not candidate.is_symlink()
            and candidate.suffix != ".gz"
        ) + [source]
        with closing(sqlite3.connect(checkpoint, timeout=2)) as database, database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("CREATE TABLE IF NOT EXISTS destination (settings TEXT)")
            database.execute(
                "CREATE TABLE IF NOT EXISTS streams (stream TEXT PRIMARY KEY, offset INTEGER)"
            )
            saved = database.execute("SELECT settings FROM destination").fetchone()
            if saved is None:
                database.execute("INSERT INTO destination VALUES (?)", (destination,))
            elif saved[0] != destination:
                raise ArchiveError("archive_destination_changed")
            for source_path in sources:
                archived, pending = _archive_file(
                    source_path, archive, database, access_events
                )
                totals["archived_events"] += archived
                totals["pending_bytes"] += pending
        typer.echo(
            json.dumps({"status": "ok", "access_events": access_events, **totals})
        )
    except ArchiveError as error:
        typer.echo(json.dumps({"status": "error", "reason": str(error)}), err=True)
        raise typer.Exit(1) from None
    except (OSError, ValueError, sqlite3.Error, BotoCoreError, ClientError):
        typer.echo('{"status":"error","reason":"audit_archive_failed"}', err=True)
        raise typer.Exit(1) from None
