import io
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
import zipfile

from player.tools.check_apk import check_apk, check_elf


def elf(alignment=16384):
    header = bytearray(120)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", header, 16, 3, 183)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    struct.pack_into("<IIQQQQQQ", header, 64, 1, 5, 0, 0, 0, 120, 120, alignment)
    return bytes(header)


class APKTests(unittest.TestCase):
    def build_apk(self, path, extension, bootstrap=None, compressed=True):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as bundle:
            item = tarfile.TarInfo("_python_bundle/modules/glm.so")
            item.size = len(extension)
            bundle.addfile(item, io.BytesIO(extension))
        compression = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
        with zipfile.ZipFile(path, "w", compression=compression) as apk:
            apk.writestr("lib/arm64-v8a/libmain.so", bootstrap or elf())
            apk.writestr("lib/arm64-v8a/libpybundle.so", data.getvalue())

    def test_aligned_apk_includes_python_extension_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.apk"
            self.build_apk(path, elf())
            self.assertEqual(check_apk(path), 2)

    def test_rejects_old_alignment_in_python_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.apk"
            self.build_apk(path, elf(4096))
            with self.assertRaisesRegex(ValueError, "glm.so.*16 KiB"):
                check_apk(path)

    def test_rejects_unaligned_uncompressed_apk_library(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.apk"
            self.build_apk(path, elf(), compressed=False)
            with self.assertRaisesRegex(ValueError, "ZIP-aligned"):
                check_apk(path)

    def test_rejects_non_elf_or_missing_native_payload(self):
        with self.assertRaises(ValueError):
            check_elf(b"not an ELF", "bad.so")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "empty.apk"
            with zipfile.ZipFile(path, "w") as apk:
                apk.writestr("assets/readme.txt", "no libraries")
            with self.assertRaisesRegex(ValueError, "missing native"):
                check_apk(path)

    def test_rejects_load_segments_with_incompatible_offsets(self):
        header = bytearray(elf())
        struct.pack_into("<Q", header, 80, 4096)
        with self.assertRaisesRegex(ValueError, "PT_LOAD"):
            check_elf(header, "bad.so")
