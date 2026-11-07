"""Restore or merge private configuration archives safely."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO

from zata_ops.private_config_backup import _list_configuration_files, create_backup

MAX_PRIVATE_ARCHIVE_SIZE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class RestoreResult:
    """Describe a preview, restore, or merge import operation.

    Attributes:
        member_paths: Relative file paths stored in the selected archive.
        added_member_paths: Paths scheduled for or completed by the operation.
        skipped_member_paths: Existing destination paths preserved during a merge.
        total_size_bytes: Total uncompressed size of the archived files.
        merge_existing: Whether the archive was merged into current configuration.
        applied: Whether the archive was written to the private directory.
        recovery_backup_path: Backup created before applying the operation, if any.
    """

    member_paths: tuple[str, ...]
    added_member_paths: tuple[str, ...]
    skipped_member_paths: tuple[str, ...]
    total_size_bytes: int
    merge_existing: bool
    applied: bool
    recovery_backup_path: Path | None


class RestoreRollbackError(RuntimeError):
    """Signal incomplete rollback when staged recovery files are retained.

    Attributes:
        staging_directory: Location of the retained rollback material.
    """

    def __init__(self, staging_directory: Path) -> None:
        """Initialize the error with the retained staging location.

        Args:
            staging_directory: Location containing staged restore and rollback files.
        """
        super().__init__("restore failed and automatic rollback was incomplete")
        self.staging_directory = staging_directory


def _validate_archive_members(
    archive_members: list[tarfile.TarInfo],
) -> tuple[tuple[tarfile.TarInfo, ...], int]:
    if not archive_members:
        raise ValueError("backup archive contains no files")

    normalized_paths: set[str] = set()
    total_size_bytes = 0
    for archive_member in archive_members:
        if not archive_member.isfile():
            raise ValueError("backup archive must contain only regular files")
        member_name = archive_member.name
        if "\\" in member_name or "\x00" in member_name:
            raise ValueError("backup archive contains an invalid path")
        member_path = PurePosixPath(member_name)
        windows_member_path = PureWindowsPath(member_name)
        if (
            member_path.is_absolute()
            or windows_member_path.is_absolute()
            or windows_member_path.drive
            or member_path.as_posix() != member_name
            or not member_path.parts
            or any(
                path_part in {"", ".", ".."}
                or ":" in path_part
                or any(
                    ord(path_character) < 32 or ord(path_character) == 127
                    for path_character in path_part
                )
                for path_part in member_path.parts
            )
            or member_path.parts[0] == "backups"
            or ".copy-reminder" in member_path.parts
            or archive_member.size < 0
        ):
            raise ValueError("backup archive contains an unsafe or reserved path")
        normalized_member_path = member_path.as_posix()
        if normalized_member_path in normalized_paths:
            raise ValueError("backup archive contains duplicate file paths")
        normalized_paths.add(normalized_member_path)
        total_size_bytes += archive_member.size
        if total_size_bytes > MAX_PRIVATE_ARCHIVE_SIZE_BYTES:
            raise ValueError("backup archive exceeds the restore size limit")

    for normalized_member_path in normalized_paths:
        member_path_parts = PurePosixPath(normalized_member_path).parts
        for parent_depth in range(1, len(member_path_parts)):
            parent_path = PurePosixPath(*member_path_parts[:parent_depth]).as_posix()
            if parent_path in normalized_paths:
                raise ValueError(
                    "backup archive has a file and directory path conflict"
                )

    return tuple(archive_members), total_size_bytes


def _extract_archive_members(
    archive: tarfile.TarFile,
    archive_members: tuple[tarfile.TarInfo, ...],
    staging_payload_directory: Path,
) -> None:
    resolved_payload_directory = staging_payload_directory.resolve()
    for archive_member in archive_members:
        member_path = PurePosixPath(archive_member.name)
        destination_path = staging_payload_directory.joinpath(*member_path.parts)
        if not destination_path.resolve().is_relative_to(resolved_payload_directory):
            raise ValueError("backup archive contains a path outside the restore area")

        for parent_depth in range(1, len(member_path.parts)):
            parent_directory = staging_payload_directory.joinpath(
                *member_path.parts[:parent_depth]
            )
            parent_directory.mkdir(mode=0o700, exist_ok=True)
            parent_directory.chmod(0o700)

        archive_stream = archive.extractfile(archive_member)
        if archive_stream is None:
            raise ValueError("backup archive file could not be read")
        with archive_stream, destination_path.open("xb") as destination_stream:
            _copy_archive_member_payload(
                archive_stream,
                destination_stream,
                expected_size_bytes=archive_member.size,
            )
        destination_path.chmod(0o600)


def _copy_archive_member_payload(
    archive_stream: BinaryIO,
    destination_stream: BinaryIO | None,
    *,
    expected_size_bytes: int,
) -> None:
    copied_size_bytes = 0
    while file_chunk_bytes := archive_stream.read(1024 * 1024):
        copied_size_bytes += len(file_chunk_bytes)
        if copied_size_bytes > expected_size_bytes:
            raise ValueError("backup archive file size did not match its header")
        if destination_stream is not None:
            destination_stream.write(file_chunk_bytes)
    if copied_size_bytes != expected_size_bytes:
        raise ValueError("backup archive file size did not match its header")


def _partition_archive_members(
    private_dir: Path,
    archive_members: tuple[tarfile.TarInfo, ...],
) -> tuple[tuple[tarfile.TarInfo, ...], tuple[tarfile.TarInfo, ...]]:
    if private_dir.exists():
        _list_configuration_files(private_dir)

    added_members = []
    skipped_members = []
    for archive_member in archive_members:
        member_path = PurePosixPath(archive_member.name)
        destination_path = private_dir.joinpath(*member_path.parts)
        path_collision = destination_path.exists() or destination_path.is_symlink()
        parent_path = destination_path.parent
        while not path_collision and parent_path != private_dir:
            if parent_path.exists() and not parent_path.is_dir():
                path_collision = True
            parent_path = parent_path.parent
        if path_collision:
            skipped_members.append(archive_member)
        else:
            added_members.append(archive_member)
    return tuple(added_members), tuple(skipped_members)


def _merge_staged_archive_members(
    private_dir: Path,
    staging_payload_directory: Path,
    archive_members: tuple[tarfile.TarInfo, ...],
    staging_directory: Path,
) -> None:
    installed_paths: list[Path] = []
    created_directories: list[Path] = []
    try:
        for archive_member in archive_members:
            member_path = PurePosixPath(archive_member.name)
            destination_path = private_dir.joinpath(*member_path.parts)
            staged_path = staging_payload_directory.joinpath(*member_path.parts)
            missing_parent_directories = []
            parent_path = destination_path.parent
            while parent_path != private_dir and not parent_path.exists():
                missing_parent_directories.append(parent_path)
                parent_path = parent_path.parent
            if parent_path.exists() and not parent_path.is_dir():
                raise ValueError("destination path is blocked by a non-directory")
            for parent_directory in reversed(missing_parent_directories):
                parent_directory.mkdir(mode=0o700)
                parent_directory.chmod(0o700)
                created_directories.append(parent_directory)

            if destination_path.exists() or destination_path.is_symlink():
                raise ValueError(
                    "destination changed during import; file already exists"
                )
            os.link(staged_path, destination_path)
            installed_paths.append(destination_path)
            staged_path.unlink()
    except BaseException as import_error:
        try:
            for installed_path in reversed(installed_paths):
                installed_path.unlink(missing_ok=True)
            for created_directory in reversed(created_directories):
                try:
                    created_directory.rmdir()
                except FileNotFoundError:
                    pass
        except BaseException as rollback_error:
            raise RestoreRollbackError(staging_directory) from rollback_error
        raise import_error


def _replace_private_configuration(
    private_dir: Path,
    staging_payload_directory: Path,
    rollback_directory: Path,
    staging_directory: Path,
) -> None:
    rollback_directory.mkdir(mode=0o700)
    moved_current_names: list[str] = []
    installed_names: list[str] = []
    try:
        for current_path in sorted(private_dir.iterdir()):
            if current_path.name in {"backups", ".copy-reminder"}:
                continue
            if current_path.is_symlink():
                raise ValueError("configuration symlinks are unsupported")
            os.replace(current_path, rollback_directory / current_path.name)
            moved_current_names.append(current_path.name)

        for staged_path in sorted(staging_payload_directory.iterdir()):
            os.replace(staged_path, private_dir / staged_path.name)
            installed_names.append(staged_path.name)
    except BaseException as restore_error:
        try:
            for installed_name in reversed(installed_names):
                installed_path = private_dir / installed_name
                if installed_path.is_symlink() or not installed_path.is_dir():
                    installed_path.unlink(missing_ok=True)
                else:
                    shutil.rmtree(installed_path)
            for moved_current_name in reversed(moved_current_names):
                os.replace(
                    rollback_directory / moved_current_name,
                    private_dir / moved_current_name,
                )
        except BaseException as rollback_error:
            raise RestoreRollbackError(staging_directory) from rollback_error
        raise restore_error

    shutil.rmtree(rollback_directory)


def restore_backup(
    backup_archive_path: Path,
    private_dir: Path,
    *,
    apply_restore: bool = False,
    merge_existing: bool = False,
) -> RestoreResult:
    """Preview, restore, or merge a private configuration archive.

    Args:
        backup_archive_path: Tar archive containing private configuration files.
        private_dir: Destination private configuration directory.
        apply_restore: Whether to apply the restore or import operation.
        merge_existing: Whether to add new files and preserve existing paths.

    Returns:
        A summary of archive paths, size, and any safety backup created.

    Raises:
        OSError: An archive, staging area, or private directory cannot be accessed.
        tarfile.TarError: The archive is not a readable tar file.
        ValueError: The archive or destination contains unsupported paths or files.
    """
    if backup_archive_path.is_symlink() or not backup_archive_path.is_file():
        raise ValueError("backup archive is missing or is a symlink")
    if private_dir.is_symlink() or (private_dir.exists() and not private_dir.is_dir()):
        raise ValueError("private configuration directory is not a regular directory")

    with tarfile.open(backup_archive_path, mode="r:gz") as archive:
        archive_members, total_size_bytes = _validate_archive_members(
            archive.getmembers()
        )
        member_paths = tuple(archive_member.name for archive_member in archive_members)
        if merge_existing:
            added_archive_members, skipped_archive_members = _partition_archive_members(
                private_dir,
                archive_members,
            )
        else:
            added_archive_members = archive_members
            skipped_archive_members = ()
        added_member_paths = tuple(
            archive_member.name for archive_member in added_archive_members
        )
        skipped_member_paths = tuple(
            archive_member.name for archive_member in skipped_archive_members
        )
        if not apply_restore or (merge_existing and not added_archive_members):
            for archive_member in archive_members:
                archive_stream = archive.extractfile(archive_member)
                if archive_stream is None:
                    raise ValueError("backup archive file could not be read")
                with archive_stream:
                    _copy_archive_member_payload(
                        archive_stream,
                        None,
                        expected_size_bytes=archive_member.size,
                    )
            return RestoreResult(
                member_paths=member_paths,
                added_member_paths=added_member_paths,
                skipped_member_paths=skipped_member_paths,
                total_size_bytes=total_size_bytes,
                merge_existing=merge_existing,
                applied=False,
                recovery_backup_path=None,
            )

        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_dir.chmod(0o700)
        backup_dir = private_dir / "backups"
        if backup_dir.is_symlink():
            raise ValueError("backup directory must not be a symlink")
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup_dir.chmod(0o700)
        reminder_path = private_dir / ".copy-reminder"
        if reminder_path.is_symlink():
            raise ValueError("reminder state must not be a symlink")

        staging_directory = Path(
            tempfile.mkdtemp(prefix=".private-restore-", dir=backup_dir)
        )
        staging_payload_directory = staging_directory / "payload"
        staging_payload_directory.mkdir(mode=0o700)
        rollback_directory = staging_directory / "previous"
        try:
            _extract_archive_members(
                archive,
                archive_members,
                staging_payload_directory,
            )
        except BaseException:
            shutil.rmtree(staging_directory, ignore_errors=True)
            raise

    preserve_staging_directory = False
    try:
        current_configuration_paths = _list_configuration_files(private_dir)
        if merge_existing:
            added_archive_members, skipped_archive_members = _partition_archive_members(
                private_dir,
                archive_members,
            )
            added_member_paths = tuple(
                archive_member.name for archive_member in added_archive_members
            )
            skipped_member_paths = tuple(
                archive_member.name for archive_member in skipped_archive_members
            )
            recovery_backup_path = (
                create_backup(
                    private_dir,
                    protected_backup_paths=(backup_archive_path,),
                )
                if current_configuration_paths and added_archive_members
                else None
            )
            if added_archive_members:
                _merge_staged_archive_members(
                    private_dir,
                    staging_payload_directory,
                    added_archive_members,
                    staging_directory,
                )
            applied = bool(added_archive_members)
        else:
            recovery_backup_path = (
                create_backup(
                    private_dir,
                    protected_backup_paths=(backup_archive_path,),
                )
                if current_configuration_paths
                else None
            )
            _replace_private_configuration(
                private_dir,
                staging_payload_directory,
                rollback_directory,
                staging_directory,
            )
            applied = True
    except RestoreRollbackError:
        preserve_staging_directory = True
        raise
    finally:
        if not preserve_staging_directory:
            shutil.rmtree(staging_directory)

    return RestoreResult(
        member_paths=member_paths,
        added_member_paths=added_member_paths,
        skipped_member_paths=skipped_member_paths,
        total_size_bytes=total_size_bytes,
        merge_existing=merge_existing,
        applied=applied,
        recovery_backup_path=recovery_backup_path,
    )
