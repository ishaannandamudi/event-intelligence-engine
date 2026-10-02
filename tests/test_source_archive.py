"""Retention, corruption, atomic publication and filesystem confinement."""

import os
from hashlib import sha256

import pytest

from eie.archive.models import ArchiveObject
from eie.archive.storage import FileArchive
from eie.core.errors import PersistenceError, ValidationError
from eie.ingestion.models import RetentionMode, RetentionPermission, RetentionPolicy

RAW = RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED)


def test_content_addressing_dedupe_changed_bytes_and_metadata_only(tmp_path):
    with FileArchive(tmp_path / "archive") as archive:
        obj = archive.write(b"original", RAW)
        assert obj.sha256 == sha256(b"original").hexdigest()
        assert archive.write(b"original", RAW) == obj and archive.read(obj) == b"original"
        assert archive.write(b"changed", RAW) != obj
        meta = archive.write(b"metadata-only", RetentionPolicy())
        assert not (archive.root / meta.sha256[:2] / meta.sha256).exists()
        with pytest.raises(PersistenceError):
            archive.read(meta)
        assert len(list(archive.root.glob("*/*"))) == 2
    archive.close()
    with pytest.raises(PersistenceError):
        archive.write(b"x", RetentionPolicy())
    with pytest.raises(PersistenceError):
        archive.read(obj)


@pytest.mark.parametrize(
    "permission", [RetentionPermission.UNKNOWN, RetentionPermission.RESTRICTED]
)
def test_raw_retention_requires_explicit_permission(permission):
    with pytest.raises(ValidationError):
        RetentionPolicy(RetentionMode.BOUNDED_RAW, permission)


def test_bounds_corrupt_and_missing_objects(tmp_path):
    with FileArchive(tmp_path, max_object_bytes=8) as archive:
        with pytest.raises(ValidationError):
            archive.write(b"123456789", RAW)
        with pytest.raises(ValidationError):
            archive.write(
                b"12", RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED, 1)
            )
        with pytest.raises(ValidationError):
            archive.write("bytes", RAW)
        with pytest.raises(ValidationError):
            archive.read(ArchiveObject("a" * 64, 9))
        obj = archive.write(b"123", RAW)
        path = tmp_path / obj.sha256[:2] / obj.sha256
        path.write_bytes(b"bad")
        with pytest.raises(PersistenceError):
            archive.read(obj)
        with pytest.raises(PersistenceError):
            archive.write(b"123", RAW)
        path.unlink()
        with pytest.raises(PersistenceError):
            archive.read(obj)


def test_root_directory_file_symlinks_and_fifo_are_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "root-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError):
        FileArchive(link)
    root = tmp_path / "archive"
    root.mkdir()
    digest = sha256(b"source").hexdigest()
    (root / digest[:2]).symlink_to(outside, target_is_directory=True)
    with FileArchive(root) as archive:
        with pytest.raises(PersistenceError):
            archive.write(b"source", RAW)
        with pytest.raises(PersistenceError):
            archive.read(ArchiveObject(digest, 6))
        assert not list(outside.iterdir())
    (root / digest[:2]).unlink()
    (root / digest[:2]).mkdir()
    target = outside / "bytes"
    target.write_bytes(b"source")
    objpath = root / digest[:2] / digest
    objpath.symlink_to(target)
    with FileArchive(root) as archive:
        with pytest.raises(PersistenceError):
            archive.read(ArchiveObject(digest, 6))
        with pytest.raises(PersistenceError):
            archive.write(b"source", RAW)
        objpath.unlink()
        os.mkfifo(objpath)
        with pytest.raises(PersistenceError):
            archive.read(ArchiveObject(digest, 6))


def test_failed_publish_removes_temporary_file(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic publish failure")

    with FileArchive(tmp_path) as archive:
        monkeypatch.setattr(os, "link", fail)
        with pytest.raises(PersistenceError):
            archive.write(b"payload", RAW)
        assert not list(tmp_path.glob("*/tmp-*"))
        assert not list(tmp_path.glob("*/*"))
