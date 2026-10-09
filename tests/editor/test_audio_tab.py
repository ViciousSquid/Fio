"""The Asset Browser's Audio tab, and Speakers playing only from its folders.

The tab shows assets/sounds or assets/music (an orange link toggles them, as
the Maps tab toggles Maps and Packages), each with the folder panel (☰) for
its subfolders. Double-clicking a sound gives it to the selected Speakers.
"""

import os

import pytest

pytest.importorskip("PyQt5", reason="the Asset Browser is a Qt widget")

from editor.asset_browser import AssetBrowser               # noqa: E402
from editor.things import Speaker                            # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def project(tmp_path):
    assets = tmp_path / "assets"
    for rel in ("textures", "sounds/ui", "music/act1"):
        (assets / rel).mkdir(parents=True)
    for rel in ("sounds/beep.wav", "sounds/ui/click.wav", "sounds/notes.txt",
                "music/theme.ogg", "music/act1/boss.mp3"):
        (assets / rel).write_bytes(b"")
    return tmp_path


@pytest.fixture
def browser(project, main_window, monkeypatch):
    monkeypatch.setattr(main_window, "root_dir", str(project))
    b = AssetBrowser(str(project / "assets" / "textures"), editor=main_window)
    yield b
    b.deleteLater()


def _names(tab):
    return [item.name_text for item in tab.items]


def test_audio_tab_shows_sounds_then_music_with_their_subfolders(browser, project):
    tabs = [browser.tabs.tabText(i) for i in range(browser.tabs.count())]
    assert tabs == ["Textures", "Models", "Maps", "Audio"]
    tab = browser.tab_audio
    assert tab.audio_mode == "sounds" and _names(tab) == ["beep.wav"]
    assert "Sounds" in tab.audio_header.text()

    tab.tree_toggle_btn.click()                          # the ☰ folder panel
    assert not tab.tree_frame.isHidden()
    root = tab.tree_view.rootIndex()
    assert os.path.samefile(tab.dir_model.filePath(root), project / "assets" / "sounds")
    tab.load_directory(str(project / "assets" / "sounds" / "ui"))
    assert _names(tab) == ["click.wav"]

    tab.toggle_audio_folder()
    assert tab.audio_mode == "music" and "Music" in tab.audio_header.text()
    assert _names(tab) == ["theme.ogg"]
    root = tab.tree_view.rootIndex()
    assert os.path.samefile(tab.dir_model.filePath(root), project / "assets" / "music")
    tab.load_directory(str(project / "assets" / "music" / "act1"))
    assert _names(tab) == ["boss.mp3"]

    tab.toggle_audio_folder()
    assert tab.audio_mode == "sounds" and _names(tab) == ["beep.wav"]


def test_a_sound_is_not_a_texture(browser):
    browser.tabs.setCurrentWidget(browser.tab_audio)
    tab = browser.tab_audio
    tab.select_item(tab.items[0])
    assert browser.get_selected_filepath() is None


def test_double_click_gives_the_sound_to_the_selected_speakers(browser, main_window, project):
    tab = browser.tab_audio
    tab.set_audio_folder("music")
    tab.load_directory(str(project / "assets" / "music" / "act1"))
    speakers = [Speaker(properties={"sound_file": ""}) for _ in range(2)]
    main_window.state.selected_objects = list(speakers)
    undo_depth = len(main_window.state.undo_stack)

    tab.activate_item(tab.items[0])
    assert [s.properties["sound_file"] for s in speakers] == ["assets/music/act1/boss.mp3"] * 2
    assert len(main_window.state.undo_stack) == undo_depth + 1


def test_with_no_speaker_selected_nothing_changes(browser, main_window):
    tab = browser.tab_audio
    main_window.state.selected_objects = []
    tab.select_item(tab.items[0])
    assert tab.assign_to_speakers() == 0


def _speaker_play():
    from editor.io_handlers import register_all_input_handlers
    from editor.io_system import IOManager
    manager = IOManager()
    register_all_input_handlers(manager)
    return manager._input_handlers[("speaker", "playsound")]


class _Logic:
    def __init__(self):
        self.sounds = []
        logic = self

        class GameState:
            def queue_sound(self, request):
                logic.sounds.append(request)

        class IO:
            def fire_output(self, *a, **k):
                pass

        self.game_state = GameState()
        self.io_manager = IO()


@pytest.mark.parametrize("value, sent", [
    ("assets/music/theme.ogg", "assets/music/theme.ogg"),
    ("beep.wav", "assets/sounds/beep.wav"),
    ("assets/textures/brick.wav", None),
    ("../../etc/x.wav", None),
])
def test_speakers_play_only_from_sounds_and_music(value, sent):
    logic = _Logic()
    speaker = Speaker(properties={"sound_file": value})
    _speaker_play()(speaker, None, logic)
    if sent is None:
        assert logic.sounds == [] and speaker.properties["state"] == "off"
    else:
        assert [r["file"] for r in logic.sounds] == [sent]


def test_the_game_view_loads_a_speakers_music(main_window, project, monkeypatch):
    view = main_window.view_3d
    loaded = {}
    monkeypatch.chdir(project)
    monkeypatch.setattr(view, "_ensure_pygame_mixer", lambda: True)

    def load(name, path):
        loaded[name] = path
        view.sound_pool[name] = object()
        return True

    monkeypatch.setattr(view, "_load_sound_to_cache", load)
    assert view._get_sound_instance("assets/music/act1/boss.mp3") is not None
    assert loaded == {"assets/music/act1/boss.mp3":
                      os.path.realpath(project / "assets" / "music" / "act1" / "boss.mp3")}
