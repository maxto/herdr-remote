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
        image = PNG + b"x" * (10 * 1024 * 1024 + 1 - len(PNG))
        with self.assertRaisesRegex(self.module.AttachmentError, "10 MiB"):
            store.save(payload(image))
        # The encoded claim is judged before anything is decoded, so an oversize
        # payload never reaches base64.
        with mock.patch.object(self.module.base64, "b64decode", side_effect=AssertionError("decoded oversize")):
            with self.assertRaisesRegex(self.module.AttachmentError, "10 MiB"):
                store.save({**payload(), "data": "A" * (14 * 1024 * 1024)})
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


class DeclarationTests(unittest.TestCase):
    """A chunked upload is judged twice: once on its claim, once on its bytes."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("test_attachment_decl", MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_a_declaration_is_judged_before_any_byte_arrives(self):
        validate = self.module.validate_declaration
        self.assertEqual(validate("a.pdf", "application/pdf", 1024), ".pdf")
        self.assertEqual(validate("a.png", "image/png", 1024), ".png")
        with self.assertRaises(self.module.AttachmentError):
            validate("a.zip", "application/zip", 10)
        with self.assertRaises(self.module.AttachmentError):
            validate("", "image/png", 10)
        with self.assertRaises(self.module.AttachmentError):
            validate("a.png", "image/png", 0)

    def test_each_type_carries_its_own_ceiling(self):
        validate = self.module.validate_declaration
        mib = 1024 * 1024
        validate("a.txt", "text/plain", mib)
        with self.assertRaises(self.module.AttachmentError):
            validate("a.txt", "text/plain", mib + 1)
        validate("a.png", "image/png", 10 * mib)
        with self.assertRaises(self.module.AttachmentError):
            validate("a.png", "image/png", 10 * mib + 1)
        validate("a.pdf", "application/pdf", 25 * mib)
        with self.assertRaises(self.module.AttachmentError):
            validate("a.pdf", "application/pdf", 25 * mib + 1)

    def test_pdf_content_needs_a_header_and_a_trailer(self):
        verify = self.module.verify_content
        verify(b"%PDF-1.4\n" + b"x" * 100 + b"\n%%EOF\n", "application/pdf")
        with self.assertRaises(self.module.AttachmentError):
            verify(b"not a pdf at all", "application/pdf")
        with self.assertRaises(self.module.AttachmentError):
            verify(b"%PDF-1.4\n" + b"x" * 2000, "application/pdf")

    def test_text_must_be_utf8_without_null_bytes_and_json_must_parse(self):
        verify = self.module.verify_content
        verify("ciao è".encode("utf-8"), "text/plain")
        verify(b"a,b\n1,2\n", "text/csv")
        with self.assertRaises(self.module.AttachmentError):
            verify(b"ciao\x00mondo", "text/plain")
        with self.assertRaises(self.module.AttachmentError):
            verify(b"\xff\xfe not utf8", "text/markdown")
        verify(b'{"a": 1}', "application/json")
        with self.assertRaises(self.module.AttachmentError):
            verify(b'{"a": ', "application/json")

    def test_image_signatures_survive_the_move_into_verify_content(self):
        """The signature checks are the existing ones, relocated, not rewritten."""
        verify = self.module.verify_content
        verify(PNG, "image/png")
        verify(JPEG, "image/jpeg")
        verify(WEBP, "image/webp")
        with self.assertRaises(self.module.AttachmentError):
            verify(JPEG, "image/png")


class UploadSinkTests(unittest.TestCase):
    """Chunks are streamed to a temporary; any break abandons the whole upload."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location("test_attachment_sink", MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "attachments"
        self.directory.mkdir(mode=0o700, parents=True)

    def sink(self, total, mime="text/plain"):
        return self.module.UploadSink(self.directory, ".txt", total=total, mime=mime)

    def test_chunks_land_in_order(self):
        sink = self.sink(6)
        sink.write(0, b"abc")
        sink.write(1, b"def")
        self.assertEqual(sink.received, 6)

    def test_a_gap_or_a_repeat_abandons_the_upload(self):
        gap = self.sink(6)
        gap.write(0, b"abc")
        with self.assertRaises(self.module.AttachmentError):
            gap.write(2, b"def")
        self.assertFalse(gap.temp_path.exists())

        repeat = self.sink(6)
        repeat.write(0, b"abc")
        with self.assertRaises(self.module.AttachmentError):
            repeat.write(0, b"abc")
        self.assertFalse(repeat.temp_path.exists())

    def test_a_sender_cannot_exceed_what_it_declared(self):
        sink = self.sink(4)
        with self.assertRaises(self.module.AttachmentError):
            sink.write(0, b"toolong")
        self.assertFalse(sink.temp_path.exists())

    def test_finishing_short_of_the_declared_total_is_refused(self):
        sink = self.sink(10)
        sink.write(0, b"abc")
        with self.assertRaises(self.module.AttachmentError):
            sink.finish()
        self.assertFalse(sink.temp_path.exists())

    def test_finish_verifies_content_and_leaves_no_temporary(self):
        sink = self.sink(8)
        sink.write(0, b"ciao\n")
        sink.write(1, b"qui")
        final = sink.finish()
        self.assertTrue(final.exists())
        self.assertEqual(final.read_bytes(), b"ciao\nqui")
        self.assertFalse(sink.temp_path.exists())

    def test_finish_refuses_bytes_that_betray_the_declared_type(self):
        sink = self.module.UploadSink(self.directory, ".pdf", total=5, mime="application/pdf")
        sink.write(0, b"nope!")
        with self.assertRaises(self.module.AttachmentError):
            sink.finish()
        self.assertFalse(sink.temp_path.exists())

    def test_abort_removes_the_temporary(self):
        sink = self.sink(3)
        sink.write(0, b"ab")
        sink.abort()
        self.assertFalse(sink.temp_path.exists())
