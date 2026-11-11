"""SysMon's VRAM line asks a driver only for what it advertises.

The NVIDIA and ATI memory queries are vendor extensions; a driver without
them answers GL_INVALID_ENUM and writes nothing, and with PyOpenGL's error
checking off -- as Fio runs -- the panel showed whatever the result buffer
held."""

import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "maps/Office_Corridor.json"


def test_a_driver_without_the_extensions_is_not_asked(fio_session, monkeypatch):
    import engine.sysmon as module
    sysmon = fio_session(MAP).view.sysmon
    sysmon._vram_extension_flags = (False, False)
    sysmon._vram_cache_time = 0
    asked = []
    monkeypatch.setattr(module.gl, "glGetIntegerv", lambda *a: asked.append(a) or 12345)
    assert sysmon._get_vram_info() == (None, None)
    assert not asked
    assert sysmon._format_memory(None, None) == "N/A"


def test_the_extensions_are_read_once_from_the_context(fio_session, monkeypatch):
    import engine.sysmon as module
    sysmon = fio_session(MAP).view.sysmon
    sysmon._vram_extension_flags = None
    names = [b"GL_ARB_something", b"GL_ATI_meminfo"]
    monkeypatch.setattr(module.gl, "glGetIntegerv", lambda name: len(names))
    monkeypatch.setattr(module.gl, "glGetStringi", lambda _, index: names[index])
    assert sysmon._vram_extensions() == (False, True)
    names.append(b"GL_NVX_gpu_memory_info")
    assert sysmon._vram_extensions() == (False, True)


def test_an_advertised_query_is_used(fio_session, monkeypatch):
    import engine.sysmon as module
    sysmon = fio_session(MAP).view.sysmon
    sysmon._vram_extension_flags = (True, False)
    sysmon._vram_cache_time = 0
    answers = {module.GL_GPU_MEM_INFO_DEDICATED_VIDMEM_NVX: 8 * 1024 * 1024,
               module.GL_GPU_MEM_INFO_CURRENT_AVAILABLE_MEM_NVX: 6 * 1024 * 1024}
    monkeypatch.setattr(module.gl, "glGetIntegerv", lambda name: answers[name])
    assert sysmon._get_vram_info() == (2048, 8192)
