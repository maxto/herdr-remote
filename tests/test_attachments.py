import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest import mock
import zlib


MODULE_PATH = Path(__file__).resolve().parents[1] / "relay" / "attachments.py"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)
# Synthetic one-pixel image fixtures; no user files are read.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////"
    "//////////////////////////////////////////////2wBDAf//////////////////"
    "//////////////////////////////////////////////////////////////////wAAR"
    "CAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAA"
    "AAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oA"
    "DAMBAAIRAxEAPwCwAB//2Q=="
)
WEBP = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvAAAAAAfQ//73v/+BiOh/AAA=")


def payload(data=PNG, mime="image/png", name="../../unsafe.png"):
    return {"name": name, "mime": mime, "data": base64.b64encode(data).decode("ascii")}


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE_PATH.exists(), "Private attachment storage is not implemented")
        spec = importlib.util.spec_from_file_location("test_attachment_store", MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "attachments"

    def test_supported_images_keep_bytes_with_private_generated_unique_names(self):
        store = self.module.AttachmentStore(self.directory)
        paths = []
        for data, mime, extension in ((PNG, "image/png", ".png"), (JPEG, "image/jpeg", ".jpg"),
                                      (WEBP, "image/webp", ".webp"), (PNG, "image/png", ".png")):
            path = store.save(payload(data, mime))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(path.parent, self.directory)
            self.assertEqual(path.suffix, extension)
            self.assertNotIn("unsafe", path.name)
            paths.append(path)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(set(paths)), 4)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_malformed_base64_unsupported_or_mismatched_mime_and_empty_image_reject(self):
        invalid = (None, [], {}, payload(b""), payload(b"not an image"), payload(PNG, "image/jpeg"),
                   payload(PNG, "image/svg+xml"), {**payload(), "data": "%%%"},
                   {**payload(), "data": [1]}, {**payload(), "name": None},
                   {**payload(), "name": "x" * 256}, payload(PNG[:8]), payload(PNG[:33]),
                   payload(b"RIFF0000WEBP"), payload(WEBP[:16] + b"\xff\xff\xff\xff" + WEBP[20:], "image/webp"))
        store = self.module.AttachmentStore(self.directory)
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(self.module.AttachmentError):
                    store.save(value)
        self.assertFalse(self.directory.exists())

    def test_decoded_and_encoded_limits_reject_before_writing(self):
        store = self.module.AttachmentStore(self.directory)
        # A valid prefix cannot bypass the byte limit.
        image = PNG + b"x" * (5 * 1024 * 1024 + 1 - len(PNG))
        with self.assertRaisesRegex(self.module.AttachmentError, "5 MiB"):
            store.save(payload(image))
        with mock.patch.object(self.module.base64, "b64decode", side_effect=AssertionError("decoded oversize")):
            with self.assertRaisesRegex(self.module.AttachmentError, "5 MiB"):
                store.save({**payload(), "data": "A" * (7 * 1024 * 1024)})
        self.assertFalse(self.directory.exists())

    def test_exactly_five_mib_image_is_accepted(self):
        extra = b"tEXt" + b"Comment\0" + b"x" * (5 * 1024 * 1024 - len(PNG) - 20)
        chunk = struct.pack(">I", len(extra) - 4) + extra + struct.pack(">I", zlib.crc32(extra))
        image = PNG[:-12] + chunk + PNG[-12:]
        self.assertEqual(len(image), 5 * 1024 * 1024)
        path = self.module.AttachmentStore(self.directory).save(payload(image))
        self.assertEqual(path.read_bytes(), image)

    def test_storage_quota_counts_existing_files_and_does_not_delete_accepted_images(self):
        store = self.module.AttachmentStore(self.directory, max_bytes=len(PNG) * 2, max_files=10)
        first = store.save(payload())
        second = store.save(payload())
        # Recreating the store cannot reset the quota.
        store = self.module.AttachmentStore(self.directory, max_bytes=len(PNG) * 2, max_files=10)
        with self.assertRaisesRegex(self.module.AttachmentError, "storage.*full"):
            store.save(payload())
        self.assertEqual({path.name for path in self.directory.iterdir()}, {first.name, second.name})
        self.assertEqual(first.read_bytes(), PNG)

    def test_file_count_quota_is_atomic_for_concurrent_uploads(self):
        store = self.module.AttachmentStore(self.directory, max_bytes=1024 * 1024, max_files=2)

        def upload(_):
            try:
                return store.save(payload())
            except self.module.AttachmentError:
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(upload, range(6)))
        self.assertEqual(sum(path is not None for path in results), 2)
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_existing_nonprivate_or_symlink_storage_is_rejected(self):
        self.directory.mkdir(mode=0o755)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(self.module.AttachmentError, "private"):
            self.module.AttachmentStore(self.directory).save(payload())
        self.directory.rmdir()
        destination = Path(self.temp.name) / "elsewhere"
        destination.mkdir(mode=0o700)
        self.directory.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(self.module.AttachmentError):
            self.module.AttachmentStore(self.directory).save(payload())
        self.assertEqual(list(destination.iterdir()), [])

    def test_partial_write_failure_removes_new_file(self):
        store = self.module.AttachmentStore(self.directory)
        with mock.patch.object(self.module.os, "fdopen", side_effect=OSError("disk failed")):
            with self.assertRaises(self.module.AttachmentError):
                store.save(payload())
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
