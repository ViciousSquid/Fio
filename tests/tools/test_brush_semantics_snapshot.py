"""The brush semantics snapshot tool uses the current per-map API."""

import json
from pathlib import Path

from tools import brush_semantics_snapshot as snapshot_tool


def test_drift_check_and_write_modes_use_current_snapshot_api(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    fixture_dir = root / "tests" / "brush_model"
    snapshot_path = tmp_path / "semantics_snapshot.json"
    write_sites_path = tmp_path / "write_sites.json"

    # Seed the temporary baselines with the committed fixtures. In
    # particular, the null digest remains an explicit unpinned marker.
    for source, target in (
        (fixture_dir / "semantics_snapshot.json", snapshot_path),
        (fixture_dir / "write_sites.json", write_sites_path),
    ):
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setattr(snapshot_tool, "SNAPSHOT", str(snapshot_path))
    monkeypatch.setattr(snapshot_tool, "WRITE_SITES", str(write_sites_path))

    # Both paths used to raise AttributeError because brush_semantics.snapshot
    # was removed while this tool still called it from _current().
    assert snapshot_tool.main([]) == 0
    assert snapshot_tool.main(["--write"]) == 0

    assert json.loads(snapshot_path.read_text(encoding="utf-8")) == json.loads(
        (fixture_dir / "semantics_snapshot.json").read_text(encoding="utf-8"))
    assert json.loads(write_sites_path.read_text(encoding="utf-8")) == json.loads(
        (fixture_dir / "write_sites.json").read_text(encoding="utf-8"))
