"""Helpers for reading audio members from ZIP-based .fiopak archives.

Archives retain project-relative member names such as assets/sounds/shot.wav;
audio is decoded from the ZIP without extracting files to disk.
"""

from __future__ import annotations

import zipfile
import zlib
from typing import BinaryIO, Optional, Tuple

try:
    import lzma
except ImportError:  # Like zipfile itself, support builds without LZMA.
    lzma = None

AUDIO_EXTENSIONS = (".wav", ".ogg", ".mp3")
# Bound encoded bytes held for eager loading; streaming music has no such cap.
MAX_AUDIO_BYTES = 256 * 1024 * 1024
_DECOMPRESSION_ERRORS = (zlib.error, EOFError)
if lzma is not None:
    _DECOMPRESSION_ERRORS += (lzma.LZMAError,)


def _members(archive, asset_root, extensions):
    return sorted({
        info.filename for info in archive.infolist()
        if not info.is_dir()
        and _valid_member(info.filename, asset_root, extensions)
    }, key=str.casefold)


def _open_member(archive, member):
    try:
        return archive.open(member, "r")
    except (RuntimeError, NotImplementedError) as exc:
        # Encrypted/unsupported members use the same error contract as corrupt
        # ZIP data, which the editor and runtime already report and skip.
        raise zipfile.BadZipFile(f"Cannot decode audio member {member!r}: {exc}") from exc


class AudioArchiveReader:
    """Own one ZIP for a preload pass; no persistent handles or stale cache.

    Use as a context manager. Reads return bytes one member at a time, so the
    preload loop need not retain a second copy of every encoded sound.
    """

    def __init__(self, archive_path, asset_root, extensions=AUDIO_EXTENSIONS):
        self.archive = zipfile.ZipFile(archive_path, "r")
        self.asset_root = asset_root
        self.extensions = extensions

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.archive.close()

    def members(self):
        return _members(self.archive, self.asset_root, self.extensions)

    def read(self, member):
        """Read a safe member, or return None for a missing/invalid name.

        Oversized, encrypted, unsupported and corrupt members raise BadZipFile.
        The size cap applies only to eager reads, not open_archive_audio streams.
        """
        if not _valid_member(member, self.asset_root, self.extensions):
            return None
        try:
            info = self.archive.getinfo(member)
        except KeyError:
            return None
        if info.is_dir():
            return None
        if info.file_size > MAX_AUDIO_BYTES:
            raise zipfile.BadZipFile(f"Audio member exceeds size limit: {member!r}")
        try:
            with _open_member(self.archive, info) as stream:
                data = stream.read(MAX_AUDIO_BYTES + 1)
        except _DECOMPRESSION_ERRORS as exc:
            raise zipfile.BadZipFile(f"Cannot decode audio member {member!r}: {exc}") from exc
        if len(data) > MAX_AUDIO_BYTES:
            raise zipfile.BadZipFile(f"Audio member exceeds size limit: {member!r}")
        return data


def _valid_member(member: str, asset_root: str,
                  extensions: tuple[str, ...]) -> bool:
    """Whether member is a safe audio path beneath asset_root."""
    if not isinstance(member, str) or not member or "\\" in member:
        return False
    if member.startswith("/"):
        return False
    parts = member.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    root = str(asset_root or "").strip("/")
    if not root or any(part in ("", ".", "..") for part in root.split("/")):
        return False
    if not member.startswith(root + "/"):
        return False
    suffixes = tuple(ext.lower() for ext in extensions)
    return bool(suffixes) and member.lower().endswith(suffixes)


def list_archive_audio(archive_path: str, asset_root: str,
                       extensions: tuple[str, ...] = AUDIO_EXTENSIONS) -> list[str]:
    """Return sorted safe audio member paths under asset_root."""
    with zipfile.ZipFile(archive_path, "r") as archive:
        return _members(archive, asset_root, extensions)


def open_archive_audio(
    archive_path: str,
    member: str,
    asset_root: str,
    extensions: tuple[str, ...] = AUDIO_EXTENSIONS,
) -> Optional[Tuple[zipfile.ZipFile, BinaryIO]]:
    """Return the owning ZIP and an open stream for one safe audio member.

    The caller must keep both objects alive while reading the stream, then close
    both. None means the member is invalid, missing or not an allowed audio file.
    """
    if not _valid_member(member, asset_root, extensions):
        return None
    archive = zipfile.ZipFile(archive_path, "r")
    try:
        try:
            info = archive.getinfo(member)
        except KeyError:
            archive.close()
            return None
        if info.is_dir():
            archive.close()
            return None
        stream = _open_member(archive, info)
    except BaseException:
        archive.close()
        raise
    return archive, stream


def read_archive_audio(
    archive_path: str,
    member: str,
    asset_root: str,
    extensions: tuple[str, ...] = AUDIO_EXTENSIONS,
) -> Optional[bytes]:
    """Read one archive audio member fully into memory (e.g. a sound effect)."""
    if not _valid_member(member, asset_root, extensions):
        return None
    with AudioArchiveReader(archive_path, asset_root, extensions) as reader:
        return reader.read(member)
