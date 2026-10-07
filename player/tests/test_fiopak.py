"""Tests for the streaming .fiopak loader (stdlib only)."""

import io
import json
import struct
import unittest
import zipfile

from player.fiopak import (
    FioPackage,
    MAX_ASSET_ENTRY_BYTES,
    MAX_DOCUMENT_BYTES,
    MAX_TOTAL_ASSET_BYTES,
    PackageError,
)
from player.tests import fixtures



def _archive_with_declared_sizes(sizes, names=None):
    """Build a real ZIP whose central-directory sizes can be oversized cheaply.

    Entries are ``assets/test<N>.bin`` unless *names* gives them.
    """
    buf = io.BytesIO()
    if names is None:
        names = [f"assets/test{index}.bin" for index in range(len(sizes))]
    names = list(zip(names, sizes))
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, _declared_size in names:
            zf.writestr(name, b"X")

    raw = bytearray(buf.getvalue())
    pos = 0
    while True:
        pos = raw.find(b"PK\x01\x02", pos)
        if pos < 0:
            break
        filename_len, extra_len, comment_len = struct.unpack_from(
            "<HHH", raw, pos + 28
        )
        name_start = pos + 46
        name_end = name_start + filename_len
        name = bytes(raw[name_start:name_end]).decode("utf-8")
        for wanted, declared_size in names:
            if name == wanted:
                raw[pos + 24:pos + 28] = struct.pack("<I", declared_size)
                break
        pos = name_end + extra_len + comment_len
    return bytes(raw)

class TestFioPackage(unittest.TestCase):
    def test_open_from_bytes_and_metadata(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        self.assertEqual(pak.title, "Test Game")
        self.assertEqual(pak.metadata["author"], "tests")

    def test_start_map_resolution(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        self.assertEqual(pak.start_map_path(), "maps/level.json")
        level = pak.load_start_map()
        self.assertEqual(level["version"], 3)
        self.assertEqual(len(level["brushes"]), 1)

    def test_context_manager_closes(self):
        with FioPackage.from_bytes(fixtures.minimal_package()) as pak:
            self.assertTrue(pak.list_maps())

    def test_list_maps_excludes_manifest(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        self.assertEqual(pak.list_maps(), ["maps/level.json"])

    def test_read_asset_direct(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        data = pak.read_asset("assets/textures/floor.png")
        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\x89PNG"))

    def test_read_asset_by_basename_fallback(self):
        # Maps reference textures by bare filename; loader must find them.
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        self.assertIsNotNone(pak.read_asset("floor.png"))

    def test_read_asset_assets_prefix_optional(self):
        # Package stores at 'tex/x.png'; request with and without assets/.
        data = fixtures.build_package(
            {"title": "P", "map_path": "maps/m.json"},
            {"maps/m.json": {"brushes": [], "things": []}},
            {"tex/x.png": b"XYZ"},
        )
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)
        self.assertEqual(pak.read_asset("tex/x.png"), b"XYZ")
        self.assertEqual(pak.read_asset("x.png"), b"XYZ")

    def test_open_asset_returns_stream(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        stream = pak.open_asset("assets/textures/floor.png")
        self.assertIsInstance(stream, io.BytesIO)
        self.assertTrue(stream.read().startswith(b"\x89PNG"))

    def test_oversized_asset_is_rejected_before_inflation(self):
        data = _archive_with_declared_sizes([MAX_ASSET_ENTRY_BYTES + 1])
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)
        self.assertIsNone(pak.read_asset("assets/test0.bin"))

    def test_cumulative_asset_budget_is_enforced(self):
        per_entry = 100 * 1024 * 1024
        count = (MAX_TOTAL_ASSET_BYTES // per_entry) + 1
        data = _archive_with_declared_sizes([per_entry] * count)
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)

        for index in range(count - 1):
            self.assertEqual(pak.read_asset(f"assets/test{index}.bin"), b"X")
        self.assertIsNone(pak.read_asset(f"assets/test{count - 1}.bin"))

    def test_missing_asset_returns_none(self):
        pak = FioPackage.from_bytes(fixtures.minimal_package())
        self.addCleanup(pak.close)
        self.assertIsNone(pak.read_asset("nope/missing.png"))
        self.assertFalse(pak.has_asset("nope/missing.png"))

    def test_legacy_manifest_json_name(self):
        data = fixtures.build_package(
            {"title": "Legacy", "map_path": "maps/m.json"},
            {"maps/m.json": {"brushes": [], "things": []}},
            manifest_name="manifest.json",
        )
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)
        self.assertEqual(pak.title, "Legacy")
        self.assertEqual(pak.start_map_path(), "maps/m.json")

    def test_start_map_autodetect_when_manifest_missing(self):
        # No manifest at all: fall back to first .json map.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("maps/only.json", json.dumps({"brushes": [], "things": []}))
        pak = FioPackage.from_bytes(buf.getvalue())
        self.addCleanup(pak.close)
        self.assertEqual(pak.start_map_path(), "maps/only.json")

    def test_stale_manifest_map_path_recovers_by_basename(self):
        data = fixtures.build_package(
            {"title": "P", "map_path": "wrong/place/level.json"},
            {"maps/level.json": {"brushes": [], "things": []}},
        )
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)
        self.assertEqual(pak.start_map_path(), "maps/level.json")

    def test_no_map_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("metadata.json", json.dumps({"title": "empty"}))
        pak = FioPackage.from_bytes(buf.getvalue())
        self.addCleanup(pak.close)
        with self.assertRaises(PackageError):
            pak.start_map_path()

    def test_bad_zip_raises(self):
        with self.assertRaises(PackageError):
            FioPackage.from_bytes(b"this is not a zip")

    def test_missing_file_raises(self):
        with self.assertRaises(PackageError):
            FioPackage.open("/no/such/package.fiopak")

    def test_real_repo_map_roundtrips(self):
        blob = fixtures.build_from_repo_map("DevTest.json")
        if blob is None:
            self.skipTest("repo maps/ not available")
        pak = FioPackage.from_bytes(blob)
        self.addCleanup(pak.close)
        level = pak.load_start_map()
        self.assertIn("brushes", level)
        self.assertIn("things", level)


class TestPackageDocumentLimits(unittest.TestCase):
    """The manifest and maps are parsed whole, so their declared size is
    checked before they are inflated."""

    def test_oversized_manifest_is_refused(self):
        data = _archive_with_declared_sizes(
            [MAX_DOCUMENT_BYTES + 1], names=["metadata.json"])
        with self.assertRaises(PackageError):
            FioPackage.from_bytes(data)

    def test_oversized_map_is_refused(self):
        data = _archive_with_declared_sizes(
            [MAX_DOCUMENT_BYTES + 1], names=["maps/huge.json"])
        pak = FioPackage.from_bytes(data)
        self.addCleanup(pak.close)
        with self.assertRaises(PackageError):
            pak.load_start_map()


if __name__ == "__main__":
    unittest.main()
