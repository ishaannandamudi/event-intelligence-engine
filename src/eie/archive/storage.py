"""Uncompressed bounded objects with immutable atomic publication and no symlink following."""

import os
import secrets
import stat
from hashlib import sha256
from pathlib import Path

from eie.archive.models import ArchiveObject
from eie.core.errors import PersistenceError, ValidationError
from eie.ingestion.models import RetentionMode, RetentionPolicy
from eie.runtime.config import positive_int


class FileArchive:
    def __init__(self, root: str | Path, *, max_object_bytes: int = 2_000_000) -> None:
        positive_int(max_object_bytes, 100_000_000)
        root = Path(root)
        if root.is_symlink():
            raise ValidationError("archive root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self.max_object_bytes = max_object_bytes
        self._fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _directory(self, digest: str, *, create: bool = False) -> int:
        ArchiveObject(digest, 0)
        if self._fd < 0:
            raise PersistenceError("archive is closed")
        prefix = digest[:2]
        if create:
            try:
                os.mkdir(prefix, mode=0o700, dir_fd=self._fd)
            except FileExistsError:
                pass
        return os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)

    def write(self, payload: bytes, policy: RetentionPolicy) -> ArchiveObject:
        if self._fd < 0:
            raise PersistenceError("archive is closed")
        if not isinstance(payload, bytes) or not isinstance(policy, RetentionPolicy):
            raise ValidationError("typed archive bytes/retention required")
        if len(payload) > self.max_object_bytes:
            raise ValidationError("archive object exceeds configured bound")
        obj = ArchiveObject(sha256(payload).hexdigest(), len(payload))
        if policy.mode is RetentionMode.METADATA_ONLY:
            return obj
        if len(payload) > policy.max_raw_bytes:
            raise ValidationError("archive retention bound exceeded")
        directory = -1
        temporary = "tmp-" + secrets.token_hex(16)
        try:
            directory = self._directory(obj.sha256, create=True)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(
                    temporary,
                    obj.sha256,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                os.fsync(directory)
            except FileExistsError:
                # Never overwrite an existing object, including a corrupt one.
                if self.read(obj) != payload:
                    raise PersistenceError("archive object collision") from None
        except OSError as exc:
            raise PersistenceError("archive publication failed") from exc
        finally:
            if directory >= 0:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
                os.close(directory)
        return obj

    def read(self, obj: ArchiveObject) -> bytes:
        if not isinstance(obj, ArchiveObject) or obj.byte_length > self.max_object_bytes:
            raise ValidationError("bounded typed archive object required")
        directory = -1
        try:
            directory = self._directory(obj.sha256)
            fd = os.open(obj.sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise PersistenceError("archive object must be a regular file")
                body = handle.read(self.max_object_bytes + 1)
            if len(body) != obj.byte_length or sha256(body).hexdigest() != obj.sha256:
                raise PersistenceError("archive integrity failure")
            return body
        except OSError as exc:
            raise PersistenceError("archive object missing or unsafe") from exc
        finally:
            if directory >= 0:
                os.close(directory)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "FileArchive":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
