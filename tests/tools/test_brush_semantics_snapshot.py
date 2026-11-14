"""The brush semantics snapshot tool uses the current per-map API."""

import json
import os

from tools import brush_semantics_snapshot as snapshot_tool


def test_drift_check_and_write_modes_use_current_snapshot_api(tmp_path, monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    fixture_dir = os.path.join(root, "tests", "brush_model")
    snapshot_path = tmp_path / "semantics_snapshot.json"
    write_sites_path = tmp_path / "write_sites.json"

    # Seed the temporary baselines with the committed fixtures. In
    # particular, the null digest remains an explicit unpinned marker.
    for source, target in (
        (os.path.join(fixture_dir, "semantics_snapshot.json"), snapshot_path),
        (os.path.join(fixture_dir, "write_sites.json"), write_sites_path),
    ):
        target.write_text(open(source, encoding="utf-8").read(), encoding="utf-8")

    monkeypatch.setattr(snapshot_tool, "SNAPSHOT", str(snapshot_path))
    monkeypatch.setattr(snapshot_tool, "WRITE_SITES", str(write_sites_path))

    # Both paths used to raise AttributeError because brush_semantics.snapshot
    # was removed while this tool still called it from _current().
    assert snapshot_tool.main([]) == 0
    assert snapshot_tool.main(["--write"]) == 0

    with open(os.path.join(fixture_dir, "semantics_snapshot.json"), encoding="utf-8") as f:
        assert json.load(snapshot_path.open(encoding="utf-8")) == json.load(f)
    with open(os.path.join(fixture_dir, "write_sites.json"), encoding="utf-8") as f:
        assert json.load(write_sites_path.open(encoding="utf-8")) == json.load(f)
