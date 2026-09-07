"""Bounded, private image storage for local agent prompts.

Accepted files remain available for agents to read asynchronously. When either
quota is reached, uploads fail without evicting files an agent may still need.
"""

import base64
import binascii
import os
from pathlib import Path
import stat
import tempfile
import threading


MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_ENCODED_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_STORAGE_BYTES = 50 * 1024 * 1024
MAX_STORAGE_FILES = 100
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


class AttachmentError(ValueError):
    """A safe, user-facing attachment failure without payload or CLI output."""


def decode_attachment(attachment):
    if not isinstance(attachment, dict):
        raise AttachmentError("Image attachment is required")
    name = attachment.get("name")
    if not isinstance(name, str) or not name or len(name) > 255:
        raise AttachmentError("Invalid image filename")
    mime = attachment.get("mime")
    if not isinstance(mime, str) or mime not in EXTENSIONS:
        raise AttachmentError("Choose a PNG, JPEG or WebP image")
    encoded = attachment.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise AttachmentError("Image data is missing or invalid")
    if len(encoded) > MAX_ENCODED_BYTES:
        raise AttachmentError("Image exceeds the 5 MiB limit")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise AttachmentError("Image data is not valid base64") from None
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentError("Image exceeds the 5 MiB limit")
    # Check the container signature, not the filename. Decoding pixels is left
    # to the agent's image reader; the relay never decompresses untrusted data.
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
    return data, EXTENSIONS[mime]


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
