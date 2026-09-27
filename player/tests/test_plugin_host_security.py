"""Security regressions for the .fiopak/plugin boundary."""

import io
import zipfile

import pytest

from player.fiopak import FioPackage, PackageError
from player.plugin_host import PlayerPluginHost


def _pak_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in entries.items():
            zf.writestr(name, raw)
    return buf.getvalue()


def test_fiopak_rejects_bundled_plugin_code():
    data = _pak_bytes({
        "metadata.json": b'{"title":"untrusted"}',
        "maps/start.json": b'{"version":3,"things":[]}',
        "plugins/evil/__init__.py": b'raise RuntimeError("pwned")',
    })
    with pytest.raises(PackageError, match="Bundled plugins are not permitted"):
        FioPackage.from_bytes(data)


@pytest.mark.parametrize(
    "entry",
    [
        "plugins/evil.py",
        r"plugins\evil.py",
        "plugins/../evil.py",
    ],
)
def test_fiopak_rejects_plugin_payload_path_forms(entry):
    data = _pak_bytes({
        "metadata.json": b'{"title":"untrusted"}',
        "maps/start.json": b'{"version":3,"things":[]}',
        entry: b"x",
    })
    with pytest.raises(PackageError, match="Bundled plugins are not permitted"):
        FioPackage.from_bytes(data)


def test_plugin_dependency_metadata_is_allowed_without_code():
    data = _pak_bytes({
        "metadata.json": b'{"title":"plain","plugins":["tidy"]}',
        "maps/start.json": b'{"version":3,"things":[]}',
    })
    with FioPackage.from_bytes(data) as package:
        assert package.required_plugins == ["tidy"]


def test_player_plugin_host_has_no_package_extraction_path():
    host = PlayerPluginHost()
    assert not hasattr(host, "_extract_plugins")
    assert not hasattr(host, "_plugin_permission_callback")
