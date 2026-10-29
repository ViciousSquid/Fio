"""Capture editor surfaces and resolved widget metrics on native Qt backends.

Run the same commit on Windows and macOS; compare_editor_ui.py joins their
manifests. No rendering context or playable world is needed for this UI audit.
"""

import argparse
import configparser
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import tempfile
import traceback
import uuid


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unnamed"


def prepare_runtime(source, output, font_size):
    # Keep junctions/symlinks outside the artifact tree so upload-artifact
    # cannot follow them and accidentally upload the entire asset library.
    runtime = Path(tempfile.mkdtemp(prefix="fio-ui-audit-"))
    for name in ("assets", "maps", "cutscenes"):
        target = runtime / name
        if target.exists():
            continue
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source / name)],
                           check=True, capture_output=True)
        else:
            target.symlink_to(source / name, target_is_directory=True)
    config = configparser.ConfigParser()
    config.read(source / "settings.ini")
    config.set("Display", "font_size", str(font_size))
    config.set("Display", "always_show_io_debug", "False")
    config.set("Editor", "autosave_enabled", "False")
    with (runtime / "settings.ini").open("w") as stream:
        config.write(stream)
    os.chdir(runtime)
    return config


class Audit:
    def __init__(self, app, output, metadata, screenshots=True):
        from PyQt5 import QtCore, QtGui, QtWidgets
        self.C, self.G, self.W = QtCore, QtGui, QtWidgets
        self.app, self.output, self.screenshots = app, output, screenshots
        self.manifest = dict(metadata, frames=[], surfaces=[], errors=[], capture_complete=False)
        self._seen = set()

    def settle(self, milliseconds=20):
        loop = self.C.QEventLoop()
        self.C.QTimer.singleShot(milliseconds, loop.quit)
        loop.exec_(self.C.QEventLoop.ExcludeUserInputEvents)

    def error(self, surface):
        message = traceback.format_exc()
        self.manifest["errors"].append({"surface": surface, "traceback": message})
        print("AUDIT_ERROR", surface, message, flush=True)
        self.save()

    def save(self):
        (self.output / "manifest.json").write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    def text(self, widget):
        for name in ("text", "title", "currentText", "placeholderText"):
            method = getattr(widget, name, None)
            if callable(method):
                try:
                    value = method()
                    if isinstance(value, str) and value:
                        return value[:300]
                except TypeError:
                    pass
        return ""

    def widgets(self, root):
        W, G = self.W, self.G
        controls = (W.QLabel, W.QAbstractButton, W.QLineEdit, W.QTextEdit,
                    W.QPlainTextEdit, W.QAbstractSpinBox, W.QComboBox,
                    W.QTabBar, W.QGroupBox, W.QAbstractItemView, W.QScrollBar,
                    W.QSlider, W.QProgressBar, W.QMenuBar, W.QToolBar)
        result = []

        def walk(parent, path):
            counts = {}
            for child in parent.findChildren(W.QWidget, options=self.C.Qt.FindDirectChildrenOnly):
                kind = child.metaObject().className()
                index = counts.get(kind, 0)
                counts[kind] = index + 1
                key = path + "/" + kind + "[" + str(index) + "]"
                if isinstance(child, controls) and child.isVisibleTo(root):
                    font = child.font()
                    info, metrics = G.QFontInfo(font), child.fontMetrics()
                    origin = child.mapTo(root, self.C.QPoint())
                    rect = child.rect().translated(origin)
                    visible = child.visibleRegion().boundingRect()
                    item = {
                        "key": key, "class": kind, "name": child.objectName(),
                        "text": self.text(child), "enabled": child.isEnabled(),
                        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
                        "visible_rect": [visible.x(), visible.y(), visible.width(), visible.height()],
                        "minimum": [child.minimumWidth(), child.minimumHeight()],
                        "size_hint": [child.sizeHint().width(), child.sizeHint().height()],
                        "font": {"requested_family": font.family(), "family": info.family(),
                                 "point_size": font.pointSizeF(), "resolved_point_size": info.pointSizeF(),
                                 "pixel_size": info.pixelSize(),
                                 "weight": font.weight(), "italic": font.italic(),
                                 "height": metrics.height(), "ascent": metrics.ascent(),
                                 "sample_width": metrics.horizontalAdvance("Ag0123 Example")},
                    }
                    # A label/button needs its full text height. Scrolling out
                    # of the viewport is recorded separately from truncation.
                    item["shorter_than_font"] = (isinstance(child, (W.QLabel, W.QAbstractButton))
                                                  and bool(item["text"])
                                                  and child.height() < metrics.height())
                    if isinstance(child, W.QTabBar):
                        item["tabs"] = [{"text": child.tabText(i),
                                         "rect": [child.tabRect(i).x(), child.tabRect(i).y(),
                                                  child.tabRect(i).width(), child.tabRect(i).height()]}
                                        for i in range(child.count())]
                    result.append(item)
                walk(child, key)
        walk(root, root.metaObject().className())
        return result

    def frame(self, name, root, metrics=True):
        if name in self._seen:
            return
        self._seen.add(name)
        # Asset thumbnails reflow after a 50ms resize debounce.
        self.settle(80 if name.startswith(('main/', 'docks/assets')) else 20)
        image_name = slug(name)[:140] + "-" + hashlib.sha1(name.encode()).hexdigest()[:8] + ".png"
        pixmap = root.grab() if self.screenshots else None
        dpr = root.devicePixelRatioF()
        if pixmap is not None:
            # Save logical-pixel images; keep the native DPR in the manifest.
            picture = pixmap.toImage().scaled(root.size(), self.C.Qt.IgnoreAspectRatio,
                                             self.C.Qt.SmoothTransformation)
            picture.setDevicePixelRatio(1)
            if not picture.save(str(self.output / image_name)):
                raise RuntimeError("Could not save " + image_name)
        self.manifest["frames"].append({
            "id": name, "image": image_name if self.screenshots else None,
            "size": [root.width(), root.height()], "dpr": dpr,
            "widgets": self.widgets(root) if metrics else [],
        })
        if len(self.manifest["frames"]) % 100 == 0:
            print("AUDIT_PROGRESS", len(self.manifest["frames"]), name, flush=True)
            self.save()

    def capture_scrolls(self, name, root):
        self.frame(name + "/top", root)
        scrolls = [s for s in root.findChildren(self.W.QScrollArea)
                   if not isinstance(root, self.W.QMainWindow)
                   and not name.startswith("docks/assets")
                   and s.isVisibleTo(root) and s.verticalScrollBar().maximum() > 0]
        for i, scroll in enumerate(scrolls):
            bar = scroll.verticalScrollBar()
            old = bar.value()
            # Capture every viewport, with overlap, rather than just the top
            # of a long tab. Metadata retains all controls at each position.
            step = max(100, round(scroll.viewport().height() * 0.85))
            positions = list(range(step, bar.maximum(), step)) + [bar.maximum()]
            for page, position in enumerate(positions):
                bar.setValue(position)
                self.frame(name + "/scroll-" + str(i) + "-page-" + str(page), root, metrics=False)
            bar.setValue(old)

    def tabs(self, name, root, scope=None):
        scope = root if scope is None else scope
        tabs = scope.findChildren(self.W.QTabWidget)
        top = []
        for tab in tabs:
            parent = tab.parentWidget()
            nested = False
            while parent is not None and parent is not scope:
                if isinstance(parent, self.W.QTabWidget):
                    nested = True
                    break
                parent = parent.parentWidget()
            if not nested and tab.isVisibleTo(root):
                top.append(tab)
        if not top:
            return
        for ti, tab in enumerate(top):
            current = tab.currentIndex()
            # Visiting each tab also constructs deferred plugin pages.
            for index in range(tab.count()):
                if not tab.isTabVisible(index):
                    continue
                label = tab.tabText(index)
                tab.setCurrentIndex(index)
                self.settle()
                child_name = name + "/tabs-" + str(ti) + "/" + str(index) + "-" + slug(label)
                self.capture_scrolls(child_name, root)
                self.tabs(child_name + "/nested", root, tab.currentWidget())
            tab.setCurrentIndex(current)

    def surface(self, name, root, wizard=False):
        # Compare an identical logical viewport, not each runner's available
        # desktop area (Cocoa otherwise clamps tool windows below menu/dock).
        if root.isWindow() and not isinstance(root, self.W.QMenu):
            root.ensurePolished()
            preferred = root.size() if root.testAttribute(self.C.Qt.WA_Resized) else root.sizeHint()
            preferred = preferred.expandedTo(root.minimumSizeHint()).expandedTo(root.minimumSize())
            root.setFixedSize(preferred)
        root.show()
        self.settle()
        before = len(self.manifest["frames"])
        self.capture_scrolls(name + "/initial", root)
        if isinstance(root, self.W.QWizard):
            original = root.startId()
            for page in root.pageIds():
                root.setStartId(page)
                root.restart()
                self.settle()
                self.capture_scrolls(name + "/page-" + str(page), root)
                self.tabs(name + "/page-" + str(page), root)
            root.setStartId(original)
            root.restart()
        else:
            self.tabs(name, root)
        self.manifest["surfaces"].append({"id": name, "frames": len(self.manifest["frames"]) - before})
        self.save()

    def attempt(self, name, factory):
        root = None
        host = None
        try:
            root = factory()
            if root is None:
                raise RuntimeError("Surface factory returned no widget")
            if not root.isWindow() and hasattr(root.parentWidget(), "_show_overlay"):
                host = root.parentWidget()
                host._show_overlay(root)
            self.surface(name, root)
        except Exception:
            self.error(name)
        finally:
            if root is not None:
                # Reject dialogs: never save edits, export, generate or restart.
                if host is not None:
                    host._close_current_overlay()
                elif isinstance(root, self.W.QDialog):
                    root.reject()
                else:
                    root.hide()

    def modal(self, name, opener, size=None):
        captured = []

        def capture():
            root = self.app.activeModalWidget()
            if root is None:
                return
            captured.append(True)
            try:
                if size is not None:
                    # exec() has already let the host desktop clamp the window.
                    root.setFixedSize(*size)
                self.surface(name, root)
            except Exception:
                self.error(name)
            finally:
                root.reject()
        self.C.QTimer.singleShot(150, capture)
        try:
            opener()
            if not captured:
                raise RuntimeError("No modal dialog was captured")
        except Exception:
            self.error(name)


def run_audit(audit, window, sections):
    """All registered property editors plus the application's authoring tools."""
    from PyQt5 import QtCore, QtWidgets
    from editor import things
    from engine import brush_geometry
    from engine.terrain import Terrain

    def enabled(name):
        return not sections or name in sections

    things._load_core_entity_types()
    entities = []
    for name, cls in sorted(things.ENTITY_TYPES.items()):
        props = {"name": "Audit " + name, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "audit://" + name))}
        try:
            entities.append((name, cls(pos=[0, 0, 0], properties=props)))
        except Exception:
            audit.error("entity-construction/" + name)
    window.state.things = [entity for _, entity in entities]
    window.scene_hierarchy.refresh_list()
    audit.manifest["entity_types"] = [name for name, _ in entities]

    if enabled("main"):
        audit.surface("main/editor", window)
        for name, root in (("docks/hierarchy", window.scene_hierarchy),
                           ("docks/assets", window.asset_browser),
                           ("docks/debug-console", window.debug_console)):
            try:
                if root is window.debug_console:
                    window.properties_tab_widget.setCurrentWidget(root)
                audit.surface(name, root)
            except Exception:
                audit.error(name)
        for large in (False, True):
            window.config.set("Display", "big_toolbar_buttons", str(large))
            window.apply_toolbar_button_size()
            audit.surface("toolbar/" + ("large" if large else "small"), window.tool_toolbar)
        window.config.set("Display", "big_toolbar_buttons", "False")
        window.apply_toolbar_button_size()

    if enabled("properties"):
        window.properties_tab_widget.setCurrentWidget(window.property_editor)
        for name, entity in entities:
            try:
                window.set_selected_objects([entity])
                audit.surface("properties/" + slug(name), window.property_editor)
            except Exception:
                audit.error("properties/" + name)
        for name, options in (("solid", {}), ("trigger", {"is_trigger": True}),
                              ("mover", {"is_mover": True}),
                              ("door", {"is_mover": True, "is_door": True}),
                              ("water", {"shader": "Water"}),
                              ("glass", {"shader": "Glass"}),
                              ("fog", {"shader": "Fog"}),
                              ("glow", {"shader": "Glow"})):
            try:
                brush = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "audit://brush-" + name)),
                         "name": "Audit " + name, "pos": [0., 0., 0.], "size": [128., 64., 128.],
                         "textures": {face: "Dev/512.jpg" for face in brush_geometry.FACE_TAGS}}
                brush.update(options)
                window.state.brushes = [brush]
                window.set_selected_objects([brush])
                audit.surface("properties/brush-" + name, window.property_editor)
            except Exception:
                audit.error("properties/brush-" + name)
        window.set_selected_objects([])

    if enabled("terrain"):
        from editor.terrain_editor import TerrainEditorPanel
        window.terrain = Terrain()
        panel = TerrainEditorPanel(window.terrain, window)
        window._show_overlay(panel)
        audit.surface("terrain", panel)
        panel._terrain_tabs.setCurrentIndex(panel._sculpt_stamp_tab_index)
        panel.set_paint_tool_mode("stamp")
        audit.capture_scrolls("terrain/stamp", panel)
        window._close_current_overlay()

    if enabled("dialogs"):
        from editor.SettingsWindow import SettingsWindow
        from editor.item_editor import ItemEditorDialog
        from editor.shortcuts_window import ShortcutsWindow
        from editor.visgroups_window import VisgroupsWindow
        from editor.surface_inspector import SurfaceInspector
        from editor.entity_inspector import EntityInspector
        from editor.monster_customise_dialog import MonsterCustomiseDialog
        from editor.cutscene_wizard import CutsceneWizard
        from editor.logic_graph_widget import LogicGraphWindow, LogicGraphScene
        from editor.logic_wizard import LogicWizard
        from editor.io_editor_widget import IOConnectionDialog
        from editor.package_dialog import PackageMetadataDialog
        from editor.procedural_generator import ProceduralMapWidget
        factories = {
            "settings": lambda: SettingsWindow(window.config, window),
            "custom-items": lambda: ItemEditorDialog(window),
            "shortcuts": lambda: ShortcutsWindow(window, window),
            "visgroups": lambda: VisgroupsWindow(window),
            "surface-inspector": lambda: SurfaceInspector(window, window),
            "entity-inspector": lambda: EntityInspector(entities[0][1], parent=window),
            "monster-sprites": lambda: MonsterCustomiseDialog(
                next(e for _, e in entities if isinstance(e, things.Monster)), window),
            "cutscenes": lambda: CutsceneWizard(window, window),
            "logic-graph": lambda: LogicGraphWindow(window.state, window),
            "logic-wizard": lambda: LogicWizard(window.state, LogicGraphScene(window.state), window),
            "io-connection": lambda: IOConnectionDialog(parent=window, entity=entities[0][1],
                                                         editor_state=window.state),
            "package-export": lambda: PackageMetadataDialog("audit.json", window),
            "procedural-generator": lambda: ProceduralMapWidget(window),
            "colour-picker": lambda: QtWidgets.QColorDialog(audit.G.QColor('#F08000'), window),
        }
        for name, factory in factories.items():
            audit.attempt("dialogs/" + name, factory)
        # Open the picker through the static helper the editor itself uses, in
        # the size it takes on any screen large enough for it (36x24 lines).
        line = audit.G.QFontMetrics(QtWidgets.QApplication.font()).height()
        audit.modal("dialogs/open-map-picker", lambda: QtWidgets.QFileDialog.getOpenFileName(
            window, 'Open Map', 'maps', 'Maps (*.json)'), size=(36 * line, 24 * line))
        audit.modal("dialogs/project-overview", window.open_project_overview)
        audit.modal("dialogs/grid-colours", window.open_grid_colours_dialog)
        audit.modal("dialogs/about", window.show_about)

    if enabled("debug"):
        from tools.debug_tables import DebugTablesWindow
        audit.attempt("debug/tables", lambda: DebugTablesWindow(window))
        overlay = window.loading_overlay
        overlay.begin("Loading audit scene", "Building terrain", 50)
        audit.surface("debug/loading", overlay)
        overlay.end()

    if enabled("menus"):
        def visit(menu, name):
            if not menu.actions():
                return
            menu.popup(window.mapToGlobal(QtCore.QPoint(100, 100)))
            audit.surface(name, menu)
            menu.hide()
            for i, action in enumerate(menu.actions()):
                if action.menu() and action.isVisible():
                    visit(action.menu(), name + "/" + str(i) + "-" + slug(action.text()))
        for action in window.menuBar().actions():
            if action.menu():
                visit(action.menu(), "menus/" + slug(action.text()))


def main():
    faulthandler.enable()
    faulthandler.dump_traceback_later(600, exit=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font-size", type=int, default=11)
    parser.add_argument("--metrics-only", action="store_true")
    parser.add_argument("--sections", default="")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source))
    random.seed(20261029)
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    os.environ.setdefault("PYOPENGL_PLATFORM", "darwin" if sys.platform == "darwin" else
                          "win32" if sys.platform == "win32" else "egl")
    config = prepare_runtime(source, output, args.font_size)

    from PyQt5 import QtCore, QtWidgets
    from editor.desktop_style import prepare_qt, install_editor_style, set_editor_font_size
    prepare_qt(config.getboolean('Display', 'high_dpi_scaling', fallback=True))
    app = QtWidgets.QApplication([])
    install_editor_style(app)
    from main import dark_stylesheet
    app.setStyleSheet(dark_stylesheet)
    set_editor_font_size(app, args.font_size)
    from plugins.manager import load_plugins
    load_plugins()
    # Only the viewport base is replaced; the editor's actual layout, controls
    # and QtGameView input/overlay initialization remain in use. This prevents
    # QOpenGLWidget from forcing a GL compositor on otherwise ordinary UI.
    native_gl_widget = QtWidgets.QOpenGLWidget
    class LayoutViewport(QtWidgets.QWidget):
        def setFormat(self, value):
            self._audit_format = value

        def makeCurrent(self):
            pass

        def doneCurrent(self):
            pass

        def context(self):
            return None

    QtWidgets.QOpenGLWidget = LayoutViewport
    try:
        from editor.main_window import MainWindow
    finally:
        QtWidgets.QOpenGLWidget = native_gl_widget
    print("AUDIT_STAGE constructing-main-window", flush=True)
    window = MainWindow(str(source))
    window.camera_movement_learned = True
    print("AUDIT_STAGE main-window-constructed", flush=True)
    # The audit exercises actual Qt widgets and native styles. Isolate the
    # renderer: Windows hosted runners don't supply a GL 3.3 graphics driver.
    view = window.view_3d
    view.setUpdatesEnabled(False)
    view.hide()
    view.setParent(window)
    placeholder = QtWidgets.QLabel("3D viewport — geometry rendering excluded from UI audit")
    placeholder.setAlignment(QtCore.Qt.AlignCenter)
    window.view_3d_dock.setWidget(placeholder)
    for timer in window.findChildren(QtCore.QTimer):
        timer.stop()
    window.setFixedSize(1440, 900)
    print("AUDIT_STAGE showing-main-window", flush=True)
    window.show()
    from PyQt5.QtGui import QCursor
    QCursor.setPos(0, 0)
    print("AUDIT_STAGE main-window-shown", flush=True)
    screen = app.primaryScreen()
    audit = Audit(app, output, {
        "platform": sys.platform, "qt_platform": app.platformName(),
        "qt_version": QtCore.QT_VERSION_STR, "python": sys.version,
        "style": app.style().objectName(), "font_size": args.font_size,
        "scale_factor": os.environ.get("QT_SCALE_FACTOR", "native"),
        "logical_dpi": [screen.logicalDotsPerInchX(), screen.logicalDotsPerInchY()],
        "native_dpr": screen.devicePixelRatio(), "native_menubar": window.menuBar().isNativeMenuBar(),
        "available_geometry": [screen.availableGeometry().width(), screen.availableGeometry().height()],
        "commit": subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip(),
        "exclusions": ["3D scene rendering and gameplay HUD", "OS file/colour pickers",
                       "arbitrary combinations of every field value"],
    }, not args.metrics_only)

    def exception_hook(kind, value, tb):
        audit.manifest["errors"].append({"surface": "qt-callback", "traceback":
            "".join(traceback.format_exception(kind, value, tb))})
        audit.save()
    sys.excepthook = exception_hook
    def perform_audit():
        try:
            print("AUDIT_STAGE visiting-surfaces", flush=True)
            run_audit(audit, window, set(filter(None, args.sections.split(","))))
            audit.manifest["capture_complete"] = True
        except Exception:
            audit.error("audit-driver")
        audit.save()
        window.unsaved_changes = False
        window.close()
        app.exit(1 if audit.manifest["errors"] else 0)

    app.setQuitOnLastWindowClosed(False)
    QtCore.QTimer.singleShot(100, perform_audit)
    app.exec_()
    print("AUDIT_COMPLETE", len(audit.manifest["frames"]), "frames,",
          len(audit.manifest["errors"]), "errors", flush=True)
    faulthandler.cancel_dump_traceback_later()
    verdict = 1 if (not audit.manifest['capture_complete'] or audit.manifest['errors']) else 0
    if __name__ == '__main__':
        # All artifacts are closed and flushed. This disposable worker opens
        # every global dialog; skip host-dependent Qt wrapper destruction
        # at interpreter shutdown, which is separate from the capture verdict.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(verdict)
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
