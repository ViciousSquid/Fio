import io
import hashlib
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
import zipfile

from player.tools.check_apk import check_apk, check_elf, check_sources


def elf(alignment=16384):
    header = bytearray(120)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", header, 16, 3, 183)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    struct.pack_into("<IIQQQQQQ", header, 64, 1, 5, 0, 0, 0, 120, 120, alignment)
    return bytes(header)


class APKTests(unittest.TestCase):
    def source_apk(self, path, sources, *, module=True):
        data = io.BytesIO()
        receipt = json.dumps({'desktop_sources_unchanged': True,
                              'source_sha256': {}, 'player_source_sha256': sources}).encode()
        with tarfile.open(fileobj=data, mode='w:gz') as bundle:
            for name, raw in [('player/runtime-build.json', receipt),
                              *([(name, b'compiled module') for name in sources] if module else [])]:
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                bundle.addfile(item, io.BytesIO(raw))
        with zipfile.ZipFile(path, 'w') as apk:
            apk.writestr('assets/private.tar', data.getvalue())

    def test_player_source_receipt_rejects_a_stale_apk(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / 'player').mkdir()
            source = project / 'player/mobile.py'
            source.write_bytes(b'mobile = True\n')
            path = project / 'test.apk'
            self.source_apk(path, {'player/mobile.py': hashlib.sha256(source.read_bytes()).hexdigest()})
            self.assertEqual(check_sources(path, project), 1)
            source.write_bytes(b'mobile = False\n')
            with self.assertRaisesRegex(ValueError, 'do not match'):
                check_sources(path, project)

    def test_player_receipt_cannot_claim_a_missing_module(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / 'player').mkdir()
            source = project / 'player/mobile.py'
            source.write_bytes(b'mobile = True\n')
            path = project / 'test.apk'
            self.source_apk(path, {'player/mobile.py': hashlib.sha256(source.read_bytes()).hexdigest()}, module=False)
            with self.assertRaisesRegex(ValueError, 'missing recorded player module'):
                check_sources(path, project)

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

    def test_rejects_accidentally_bundled_showcase_world(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.apk'
            self.build_apk(path, elf())
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode='w:gz') as private:
                entry = tarfile.TarInfo('player/_SHOWCASE.fiopak')
                private.addfile(entry)
            with zipfile.ZipFile(path, 'a') as apk:
                apk.writestr('assets/private.tar', data.getvalue())
            with self.assertRaisesRegex(ValueError, 'must import world assets'):
                check_apk(path)
