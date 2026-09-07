"""Bounded, private attachment storage for local agent prompts.

Accepted files remain available for agents to read asynchronously. When either
quota is reached, uploads fail without evicting files an agent may still need.
"""

import base64
import binascii
import json
import os
from pathlib import Path
import stat
import tempfile
import threading


MAX_STORAGE_BYTES = 200 * 1024 * 1024
MAX_STORAGE_FILES = 100
_MIB = 1024 * 1024

EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt", "text/markdown": ".md",
    "text/csv": ".csv", "application/json": ".json",
}
# A ceiling per kind, not one for everything: a note is not a scan.
LIMITS = {
    "image/png": 10 * _MIB, "image/jpeg": 10 * _MIB, "image/webp": 10 * _MIB,
    "application/pdf": 25 * _MIB,
    "text/plain": _MIB, "text/markdown": _MIB,
    "text/csv": _MIB, "application/json": _MIB,
}
TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}


def encoded_ceiling(mime):
    """Base64 inflates by four thirds; refuse the claim before decoding it."""
    return 4 * ((LIMITS[mime] + 2) // 3)


def validate_declaration(name, mime, size):
    """Judge a file before a byte of it arrives; return the extension to use."""
    if not isinstance(name, str) or not name or len(name) > 255:
        raise AttachmentError("Invalid attachment filename")
    if not isinstance(mime, str) or mime not in EXTENSIONS:
        raise AttachmentError("Choose an image, a PDF or a text file")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise AttachmentError("Invalid attachment size")
    if size > LIMITS[mime]:
        raise AttachmentError(f"This file type is limited to {LIMITS[mime] // _MIB} MiB")
    return EXTENSIONS[mime]


def verify_content(data, mime):
    """Judge assembled bytes against the type their sender claimed.

    The browser's MIME is often empty or wrong, so the bytes decide. Containers
    are checked, never decompressed: the relay does not parse untrusted media.
    """
    if mime not in EXTENSIONS:
        raise AttachmentError("Choose an image, a PDF or a text file")
    if mime == "application/pdf":
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise AttachmentError("This file does not look like a PDF")
        return
    if mime in TEXT_TYPES:
        if b"\x00" in data:
            raise AttachmentError("A text attachment cannot contain null bytes")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise AttachmentError("A text attachment must be UTF-8") from None
        if mime == "application/json":
            try:
                json.loads(text)
            except ValueError:
                raise AttachmentError("This file is not valid JSON") from None
        return
    valid = {
        "image/png": (
            len(data) >= 57 and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
            and int.from_bytes(data[16:20], "big") > 0 and int.from_bytes(data[20:24], "big") > 0
        ),
        "image/jpeg": len(data) >= 4 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"),
        "image/webp": (
            len(data) >= 20 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
            and int.from_bytes(data[4:8], "little") == len(data) - 8
            and 0 < int.from_bytes(data[16:20], "little") <= len(data) - 20
        ),
    }[mime]
    if not valid:
        raise AttachmentError("Image data does not match its PNG, JPEG or WebP type")


class AttachmentError(ValueError):
    """A safe, user-facing attachment failure without payload or CLI output."""


def decode_attachment(attachment):
    """Decode and judge a whole attachment delivered in one message."""
    if not isinstance(attachment, dict):
        raise AttachmentError("Attachment is required")
    name = attachment.get("name")
    mime = attachment.get("mime")
    if not isinstance(mime, str) or mime not in EXTENSIONS:
        raise AttachmentError("Choose an image, a PDF or a text file")
    encoded = attachment.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise AttachmentError("Attachment data is missing or invalid")
    ceiling = LIMITS[mime] // (1024 * 1024)
    if len(encoded) > encoded_ceiling(mime):
        raise AttachmentError(f"This file type is limited to {ceiling} MiB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("Attachment data is not valid base64") from None
    if len(data) > LIMITS[mime]:
        raise AttachmentError(f"This file type is limited to {ceiling} MiB")
    extension = validate_declaration(name, mime, len(data))
    verify_content(data, mime)
    return data, extension


class AttachmentStore:
    def __init__(self, directory, *, max_bytes=MAX_STORAGE_BYTES, max_files=MAX_STORAGE_FILES):
        self.directory = Path(directory).expanduser().absolute()
        self.max_bytes = max_bytes
        self.max_files = max_files
        self._lock = threading.Lock()

    def save(self, attachment):
        data, extension = decode_attachment(attachment)
        with self._lock:
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = self.directory.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise AttachmentError("Image storage must be a private directory")
                if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
                    raise AttachmentError("Image storage must be a private owner-only directory")
                files = list(self.directory.iterdir())
                total_bytes = sum(path.lstat().st_size for path in files)
                if len(files) >= self.max_files or total_bytes + len(data) > self.max_bytes:
                    raise AttachmentError("Image storage is full; ask the relay owner to remove old attachments")
                fd, filename = tempfile.mkstemp(prefix="image-", suffix=extension, dir=self.directory)
                path = Path(filename)
                try:
                    stream = os.fdopen(fd, "wb")
                    fd = None  # The stream now owns the descriptor.
                    with stream:
                        stream.write(data)
                except Exception:
                    if fd is not None:
                        os.close(fd)
                    path.unlink(missing_ok=True)
                    raise
                return path
            except OSError:
                raise AttachmentError("Private image storage is unavailable") from None
