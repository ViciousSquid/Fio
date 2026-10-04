"""
Editor-side integration shim for the Fio plugin system.

The **engine** play lifecycle is wired natively: ``engine.logic_thread.LogicThread``
calls the plugin manager directly (attach at ``__init__``, play-start/stop in
``LogicSession.apply_play_mode``, per-tick dispatch in ``_tick_play_mode``). Nothing in this
module touches the engine any more.

What remains here are the **editor** integrations, kept as small guarded
monkey-patches so the large editor source files stay untouched. Adding the
``plugins/`` package plus a one-line bootstrap in ``editor/__init__.py`` is all
the editor needs.

``apply()`` is idempotent and defensive: any patch that cannot be installed
(e.g. a module that fails to import in a headless/tool context) is skipped with
a log line rather than breaking startup. It patches:

* ``editor.editor_state.EditorState``
    - ``load_from_data``      → auto-enable plugins a loaded level's entities need
    - ``clear_scene``         → revert a level-driven auto-enable (File ▸ New)
* ``editor.view_2d.View2D``
    - ``contextMenuEvent``    → add a "Plugins ▸ <plugin>" placement submenu,
                                reusing the original menu handler unchanged
* ``editor.ui.Ui_MainWindow``
    - ``create_menu_bar``     → add a top-level **Plugins** menu bar entry

Package export is not patched either: a ``.fiopak`` is a world container and
never carries plugin code, so the exporter has nothing plugin-specific to do.

The equivalent hand-edits (for reference / an alternative to this shim) would
be small insertions in those files; see ``plugins/README.md``.
"""


from __future__ import annotations

from PyQt5.QtWidgets import QMenu

_applied = False


def _log(message: str):
    try:
        from editor.debug_console import debug_log
        debug_log("Plugins", message)
    except Exception:
        print(f"[Plugins] {message}")


def apply():
    """Install all plugin integration patches. Safe to call more than once."""
    global _applied
    if _applied:
        return
    _applied = True
    _patch_editor_menu()
    _patch_property_editor()


# ---------------------------------------------------------------------------
# engine.logic_thread.LogicThread
# ---------------------------------------------------------------------------
#
# The play-lifecycle hooks (runtime attach, play-start/stop, per-tick dispatch)
# are wired natively inside ``engine.logic_thread.LogicThread`` — see the guarded
# ``self.plugins`` calls in ``__init__`` and ``_tick_play_mode``; lifecycle dispatch
# is performed by ``LogicSession.apply_play_mode``.
# No monkey-patch is needed here; the engine calls the plugin manager directly.


# ---------------------------------------------------------------------------
# editor.ui.Ui_MainWindow  — top-level "Plugins" menu bar entry
# ---------------------------------------------------------------------------

def _patch_editor_menu():
    try:
        from editor.ui import Ui_MainWindow
    except Exception as exc:
        _log(f"menu-bar patch skipped ({exc})")
        return

    if getattr(Ui_MainWindow, "_fio_plugins_patched", False):
        return

    _orig_create_menu_bar = Ui_MainWindow.create_menu_bar

    def create_menu_bar(self, MainWindow):
        _orig_create_menu_bar(self, MainWindow)
        try:
            _build_plugins_menu(MainWindow)
        except Exception as exc:
            _log(f"Plugins menu build failed: {exc}")

    Ui_MainWindow.create_menu_bar = create_menu_bar
    Ui_MainWindow._fio_plugins_patched = True


def _disabled_from_config(MainWindow):
    """Read the persisted set of disabled plugin names from settings.ini."""
    cfg = getattr(MainWindow, "config", None)
    if cfg is None:
        return set()
    try:
        raw = cfg.get("Plugins", "disabled", fallback="")
    except Exception:
        raw = ""
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def _persist_disabled(MainWindow):
    """Write the current disabled-plugin set back to settings.ini."""
    from plugins.manager import get_manager
    cfg = getattr(MainWindow, "config", None)
    if cfg is None:
        return
    mgr = get_manager()
    disabled = sorted(mgr.plugin_package_name(p).lower()
                      for p in mgr.plugins if not mgr.is_enabled(p))
    try:
        if not cfg.has_section("Plugins"):
            cfg.add_section("Plugins")
        cfg.set("Plugins", "disabled", ", ".join(disabled))
        if hasattr(MainWindow, "save_config"):
            MainWindow.save_config()
    except Exception as exc:
        _log(f"could not persist plugin toggle: {exc}")


def _build_plugins_menu(MainWindow):
    from PyQt5.QtWidgets import QMessageBox
    from plugins.manager import get_manager

    mgr = get_manager()

    # Apply any persisted enable/disable choices before drawing the menu.
    # Through set_enabled, not the attribute: the manager caches which plugins
    # tick and dispatch keyed on its enabled generation, and a plugin is told
    # (on_enabled_changed) so it can release what it holds.
    persisted_off = _disabled_from_config(MainWindow)
    for plugin in mgr.plugins:
        if mgr.plugin_package_name(plugin).lower() in persisted_off or \
                plugin.name.lower() in persisted_off:
            mgr.set_enabled(plugin, False)

    menubar = MainWindow.menuBar()

    # Insert Plugins immediately before Help
    help_action = None
    for action in menubar.actions():
        if action.text().replace("&", "") == "Help":
            help_action = action
            break

    # Drop a previously built menu, so a rebuild never leaves two "Plugins"
    # entries in the menu bar.
    for action in list(menubar.actions()):
        if action.text().replace("&", "") == "Plugins":
            menubar.removeAction(action)

    menu = QMenu("Plugins", menubar)
    if help_action:
        menubar.insertMenu(help_action, menu)
    else:
        menubar.addMenu(menu)

    if not mgr.plugins:
        act = menu.addAction("No plugins loaded")
        act.setEnabled(False)
        return

    # (plugin, "Enabled" toggle, plugin-owned actions) per submenu, re-read
    # from the live enabled state every time the menu opens. The menu is built
    # once at startup, but a plugin's state changes after that without the
    # menu being involved -- above all a level that auto-enables its plugin
    # (Big World for a map with a BigWorldSettings entity). Without the
    # refresh the toggle kept showing that plugin as off while it ran.
    rows = []

    def _sync_with_live_state():
        for plugin, toggle, actions in rows:
            on = mgr.is_enabled(plugin)
            if toggle.isChecked() != on:
                # Reflect the state; do not fire _toggle_plugin, which would
                # persist it and turn an auto-enable into a manual one.
                toggle.blockSignals(True)
                toggle.setChecked(on)
                toggle.blockSignals(False)
            for act in actions:
                act.setVisible(on)
                act.setEnabled(on)

    menu.aboutToShow.connect(_sync_with_live_state)

    for plugin in mgr.plugins:
        sub = menu.addMenu(plugin.name)

        # Plugin-owned actions sit at the very top, and are hidden outright
        # while that plugin is off rather than shown greyed out.
        # _run_plugin_menu_action refuses to call them without their plugin, so
        # a visible one is an offer the editor cannot honour -- Tidy's "Load
        # Demo map" looked available and silently did nothing.
        plugin_actions = []
        enabled = mgr.is_enabled(plugin)
        for label, callback, tooltip in [
            (label, callback, tooltip)
            for pl, label, callback, tooltip in mgr.menu_actions()
            if pl is plugin
        ]:
            act = sub.addAction(label)
            if tooltip:
                act.setToolTip(tooltip)
            act.triggered.connect(
                lambda _checked=False, p=plugin, cb=callback:
                _run_plugin_menu_action(MainWindow, p, cb))
            plugin_actions.append(act)
        if plugin_actions:
            # The separator belongs to the group: with the actions hidden it
            # would otherwise sit above "Enabled" on its own.
            plugin_actions.append(sub.addSeparator())
        for act in plugin_actions:
            act.setVisible(enabled)
            act.setEnabled(enabled)

        # Enable/disable toggle (checked = on).
        toggle = sub.addAction("Enabled")
        toggle.setCheckable(True)
        toggle.setChecked(mgr.is_enabled(plugin))
        toggle.toggled.connect(
            lambda checked, p=plugin, acts=plugin_actions:
            _toggle_plugin(MainWindow, p, checked, acts))
        rows.append((plugin, toggle, plugin_actions))
        sub.addSeparator()

        # Placement entries for this plugin's entities.
        entries = [(label, cls) for pl, label, cls in mgr.menu_entries()
                   if pl is plugin]
        if entries:
            place_hdr = sub.addAction("Add entity (at origin):")
            place_hdr.setEnabled(False)
            for label, cls in entries:
                act = sub.addAction(f"   {label}")
                act.triggered.connect(
                    lambda _checked=False, c=cls, l=label, p=plugin:
                    _place_plugin_entity(MainWindow, p, c, l))
            sub.addSeparator()

        about = sub.addAction("About…")
        about.triggered.connect(
            lambda _checked=False, p=plugin:
            QMessageBox.information(
                MainWindow, f"{p.name} v{p.version}",
                f"{p.description or '(no description)'}\n\n"
                f"Version: {p.version}\n"
                f"Category: {p.category}\n"
                f"Place its entities from here or the 2D view's right-click "
                f"menu under Plugins ▸ {p.name}."))



def _run_plugin_menu_action(MainWindow, plugin, callback):
    """Invoke a plugin-owned editor menu action safely."""
    if not getattr(plugin, "enabled", False):
        return
    try:
        callback(MainWindow)
    except Exception as exc:
        _log(f"plugin menu action failed for '{plugin.name}': {exc}")


def _toggle_plugin(MainWindow, plugin, enabled, menu_actions=None):
    from plugins.manager import get_manager
    get_manager().set_enabled(plugin, enabled)
    for action in menu_actions or ():
        try:
            action.setVisible(bool(enabled))
            action.setEnabled(bool(enabled))
        except Exception:
            pass
    _persist_disabled(MainWindow)
    if hasattr(MainWindow, "show_toast"):
        state = "enabled" if enabled else "disabled"
        MainWindow.show_toast(f"Plugin '{plugin.name}' {state}"
                              + ("" if enabled else " (restart to fully unload)"))


def singleton_instance(things, ttype):
    """The entity in *things* that a new *ttype* entity would duplicate.

    None unless *ttype* is a registered per-map singleton (see
    ``register_singleton_entity``) and *things* already holds one. Fully
    guarded -- any error means "no conflict", so an ordinary entity is never
    affected. Placement, clone and paste all ask this.
    """
    if not ttype:
        return None
    try:
        from plugins.manager import get_manager
        mgr = get_manager()
        if not mgr.is_singleton_entity(ttype):
            return None
        norm = mgr._normalise_type(ttype)
    except Exception:
        return None
    for t in things or []:
        props = getattr(t, "properties", None)
        if not isinstance(props, dict):
            continue
        try:
            if mgr._normalise_type(props.get("type", "")) == norm:
                return t
        except Exception:
            continue
    return None


def _singleton_blocked(main_window, editor_state, ttype) -> bool:
    """Enforce per-map singleton entity types (see ``register_singleton_entity``).

    If *ttype* is a registered singleton and one already exists in the scene,
    select the existing instance, toast, and return True so the caller aborts
    placement. Otherwise returns False. Fully guarded -- any error means "don't
    block", so an ordinary entity is never affected.
    """
    existing = singleton_instance(getattr(editor_state, "things", []), ttype)
    if existing is None:
        return False
    try:
        if hasattr(main_window, "set_selected_object"):
            main_window.set_selected_object(existing)
        if hasattr(main_window, "update_views"):
            main_window.update_views()
        if hasattr(main_window, "show_toast"):
            main_window.show_toast(
                "Only one of this entity is allowed per map - selected the existing one.",
                is_error=True)
    except Exception as exc:
        _log(f"singleton select failed ({exc})")
    return True


def _place_plugin_entity(MainWindow, plugin, cls, label):
    """Create a plugin entity at the origin and select it."""
    from plugins.manager import get_manager
    if not get_manager().is_enabled(plugin):
        if hasattr(MainWindow, "show_toast"):
            MainWindow.show_toast(f"Plugin '{plugin.name}' is disabled",
                                  is_error=True)
        return
    try:
        # Probe and singleton-check BEFORE save_state, so a refused placement
        # never leaves a spurious entry on the undo stack.
        probe = cls(pos=[0, 40, 0])
        ttype = probe.properties.get("type") if hasattr(probe, "properties") else None
        if _singleton_blocked(MainWindow, MainWindow.state, ttype):
            return
        if hasattr(MainWindow, "save_state"):
            MainWindow.save_state()
        thing = probe
        MainWindow.state.things.append(thing)
        if hasattr(MainWindow, "set_selected_object"):
            MainWindow.set_selected_object(thing)
        if hasattr(MainWindow, "update_views"):
            MainWindow.update_views()
        if hasattr(MainWindow, "show_toast"):
            MainWindow.show_toast(f"Added {label} at origin — drag it into place")
    except Exception as exc:
        _log(f"menu placement failed: {exc}")


# ---------------------------------------------------------------------------
# editor.property_editor.PropertyEditor  — typed widgets from a property schema
# ---------------------------------------------------------------------------

def _patch_property_editor():
    """Render plugin-declared property schemas with fitting widgets.

    For a plugin entity whose ``type`` has a registered schema (see
    ``EditorAPI.register_properties`` / ``FioPlugin.describe_properties``), this
    wraps ``PropertyEditor._iterate_thing_properties`` to draw enum dropdowns,
    ranged spin boxes, checkboxes and labelled/tool-tipped fields instead of
    guessing from the stored value's Python type. Anything without a schema —
    including every built-in entity — falls through to the original method
    unchanged, so the patch is additive and safe.
    """
    try:
        from editor.property_editor import PropertyEditor
    except Exception as exc:
        _log(f"property-editor patch skipped ({exc})")
        return

    if getattr(PropertyEditor, "_fio_plugins_patched", False):
        return

    from plugins.manager import get_manager

    _orig_iterate = PropertyEditor._iterate_thing_properties

    def _iterate_thing_properties(self, form, thing, property_keys=None):
        specs = owner = ttype = mgr = None

        try:
            props = getattr(thing, "properties", None)
            ttype = props.get("type") if isinstance(props, dict) else None

            if ttype:
                mgr = get_manager()
                specs = mgr.property_schema_for(ttype)
                owner = mgr.plugin_for_type(ttype)
        except Exception:
            specs = owner = None

        # Plugin-owned entity with a full schema gets its schema-driven widgets.
        # Otherwise use the normal PropertyEditor iterator. Pass property_keys
        # through so the editor's explicit primary/advanced classification is
        # preserved even when this plugin shim is installed.
        rendered = False

        if specs and owner is not None:
            try:
                # A plugin schema owns the complete rendering of its properties.
                # Explicit property filtering is therefore not applied here.
                _render_schema_rows(self, form, thing, specs)
                rendered = True
            except Exception as exc:
                _log(
                    f"schema render failed for "
                    f"'{getattr(thing, 'name', '?')}', falling back ({exc})"
                )

        if not rendered:
            _orig_iterate(
                self,
                form,
                thing,
                property_keys=property_keys,
            )

        try:
            extra = (
                mgr.extra_fields_for(ttype)
                if (mgr and ttype)
                else []
            )

            if extra:
                _append_extra_fields(
                    self,
                    form,
                    thing,
                    extra,
                )
        except Exception as exc:
            _log(f"extra-field render failed ({exc})")

    PropertyEditor._iterate_thing_properties = _iterate_thing_properties

    # Property sections (API 1.5.0): small editors that belong with an entity's
    # own properties rather than in a tab of their own. They go at the end of
    # the Properties tab as collapsible sections, each built the first time it
    # is opened, so a collapsed section costs nothing.
    _orig_props_tab = PropertyEditor._create_thing_properties_tab

    def _create_thing_properties_tab(self, thing):
        tab = _orig_props_tab(self, thing)
        try:
            props = getattr(thing, "properties", None)
            ttype = props.get("type") if isinstance(props, dict) else None
            sections = get_manager().property_sections_for(ttype) if ttype else []
            layout = tab.layout() if (sections and tab is not None) else None
            if layout is None:
                return tab
            from editor.property_editor import CollapsibleSection

            for label, factory, expanded in sections:
                section = CollapsibleSection(label, expanded=expanded)
                section.setObjectName(f"fio_property_section:{label}")
                _wire_lazy_section(section, factory, thing, label)
                # Before the tab's trailing stretch, so sections pack to the top.
                layout.insertWidget(max(0, layout.count() - 1), section)
        except Exception as exc:
            _log(f"property sections failed ({exc})")
        return tab

    PropertyEditor._create_thing_properties_tab = _create_thing_properties_tab

    # Custom property tabs: append plugin tabs after the stock ones are built.
    _orig_populate = PropertyEditor.populate_for_thing

    def populate_for_thing(self, thing):
        _orig_populate(self, thing)
        try:
            props = getattr(thing, "properties", None)
            ttype = props.get("type") if isinstance(props, dict) else None
            tabs = get_manager().property_tabs_for(ttype) if ttype else []
            widget = getattr(self, "tab_widget", None)
            if not tabs or widget is None:
                return
            # Lazy tab construction: each custom tab's factory builds a full,
            # often heavy widget. Building all of them on every selection is the
            # bulk of the panel's sluggishness, and most are never looked at. So
            # insert a light placeholder per tab now and build the real content
            # the first time that tab is actually shown.
            try:
                from PyQt5.QtWidgets import QWidget, QVBoxLayout
            except Exception:
                # No Qt (headless) - fall back to eager build so behaviour holds.
                for label, factory in tabs:
                    try:
                        widget.addTab(factory(thing), label)
                    except Exception as exc:
                        _log(f"custom tab '{label}' failed ({exc})")
                return

            pending = {}
            for label, factory in tabs:
                placeholder = QWidget()
                lay = QVBoxLayout(placeholder)
                lay.setContentsMargins(0, 0, 0, 0)
                idx = widget.addTab(placeholder, label)
                pending[idx] = (placeholder, factory)
            widget._fio_pending_tabs = pending

            def _build_pending(index, w=widget, th=thing):
                p = getattr(w, "_fio_pending_tabs", None)
                if not p or index not in p:
                    return
                placeholder, factory = p.pop(index)
                try:
                    inner = factory(th)
                except Exception as exc:
                    _log(f"custom tab build failed ({exc})")
                    return
                if inner is not None:
                    placeholder.layout().addWidget(inner)

            widget.currentChanged.connect(_build_pending)
            # If a custom tab happens to be the current one (e.g. a restored tab
            # index), build it now so it isn't left blank.
            _build_pending(widget.currentIndex())
        except Exception as exc:
            _log(f"custom property tabs failed ({exc})")

    PropertyEditor.populate_for_thing = populate_for_thing
    PropertyEditor._fio_plugins_patched = True


def _wire_lazy_section(section, factory, thing, label):
    """Build a property section's content the first time it is opened.

    A section's factory can be as heavy as a tab's, and a collapsed section is
    not being looked at, so nothing is built until it is expanded. One that
    starts expanded is built immediately.
    """
    state = {"built": False}

    def _build(*_args):
        if state["built"]:
            return
        state["built"] = True
        try:
            inner = factory(thing)
        except Exception as exc:
            _log(f"property section '{label}' failed ({exc})")
            return
        if inner is not None:
            section.addWidget(inner)

    section.toggle.toggled.connect(lambda checked: checked and _build())
    if section.toggle.isChecked():
        _build()


def _append_extra_fields(editor_self, form, thing, specs):
    """Append plugin-registered extra fields to *thing*'s property form."""
    for spec in specs:
        if spec.name in ("name", "id", "type", "_io_connections"):
            continue
        value = thing.properties.get(spec.name, spec.default)
        label = (spec.label or spec.name.replace("_", " ").title()) + ":"
        widget = _widget_for_spec(editor_self, thing, spec, value)
        if getattr(spec, "help", ""):
            try:
                widget.setToolTip(spec.help)
            except Exception:
                pass
        form.addRow(label, widget)


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _widget_for_spec(editor_self, thing, spec, value):
    """Build the widget for one ``PropertySpec`` bound to ``update_object_prop``."""
    from editor.property_editor import _make_combo, _make_spin, _make_checkbox
    from PyQt5.QtWidgets import QLineEdit

    t = (getattr(spec, "type", "string") or "string").lower()
    name = spec.name

    if t == "enum" and spec.choices:
        choices = [str(c) for c in spec.choices]
        default = spec.default if spec.default is not None else (choices[0] if choices else "")
        cur = str(value if value is not None else default)
        if cur not in choices:
            choices = [cur] + choices
        return _make_combo(choices, cur,
                           lambda txt, k=name: editor_self.update_object_prop(k, txt))

    if t == "bool":
        bv = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
        return _make_checkbox("", bv,
                              lambda c, k=name: editor_self.update_object_prop(k, c))

    if t == "int":
        lo = int(spec.min) if spec.min is not None else -99999
        hi = int(spec.max) if spec.max is not None else 99999
        try:
            iv = int(float(value))
        except (TypeError, ValueError):
            iv = int(spec.default) if isinstance(spec.default, (int, float)) else 0
        spin = _make_spin(iv, lo, hi)
        spin.editingFinished.connect(
            lambda w=spin, k=name: editor_self.update_object_prop(k, w.value()))
        return spin

    if t == "float":
        inp = QLineEdit("" if value is None else str(value))
        inp.editingFinished.connect(
            lambda le=inp, k=name: editor_self.update_object_prop(k, _to_float(le.text())))
        return inp

    # string / asset / vec3 and anything else → plain text field.
    inp = QLineEdit("" if value is None else str(value))
    inp.editingFinished.connect(
        lambda le=inp, k=name: editor_self.update_object_prop(k, le.text()))
    return inp


def _section_header(text):
    """A bold, boxed section heading spanning a QFormLayout row.

    Font sizing uses the widget's point-based font (not a px stylesheet value)
    so it stays crisp and correctly sized on high-DPI displays; only colour and
    border come from the stylesheet.
    """
    from PyQt5.QtWidgets import QLabel
    lbl = QLabel(text.upper())
    f = lbl.font()
    f.setBold(True)
    f.setLetterSpacing(f.PercentageSpacing, 108)
    lbl.setFont(f)
    lbl.setStyleSheet(
        "color:#F08000; border:none; border-bottom:1px solid #555;"
        "margin-top:8px; padding:3px 0 2px 0;")
    return lbl


def _render_schema_rows(editor_self, form, thing, specs):
    """Draw schema-driven rows first, then any remaining properties generically.

    Specs carrying a ``group`` are rendered under a section heading, turning a
    flat property list into an organised panel. Specs with no group behave
    exactly as before.
    """
    from editor.property_editor import _make_spin, _make_checkbox
    from PyQt5.QtWidgets import QLineEdit

    # An entity class can keep plumbing out of its panel -- e.g. the marker
    # sprite a settings entity draws with -- by listing the keys in
    # EDITOR_HIDDEN_PROPERTIES. They are still stored and saved as usual.
    _HIDDEN = ("name", "id", "type", "_io_connections") + tuple(
        getattr(thing, "EDITOR_HIDDEN_PROPERTIES", ()) or ())
    covered = set()
    current_group = None

    for spec in specs:
        if spec.name in _HIDDEN:
            continue
        covered.add(spec.name)
        group = getattr(spec, "group", "") or ""
        if group and group != current_group:
            current_group = group
            form.addRow(_section_header(group))
        value = thing.properties.get(spec.name, spec.default)
        label = (spec.label or spec.name.replace("_", " ").title()) + ":"
        widget = _widget_for_spec(editor_self, thing, spec, value)
        if getattr(spec, "help", ""):
            try:
                widget.setToolTip(spec.help)
            except Exception:
                pass
        form.addRow(label, widget)

    # Anything the schema didn't mention still gets an editor, inferred from its
    # current value's type — so declaring a partial schema never hides a field.
    _uncovered = [(k, v) for k, v in sorted(thing.properties.items())
                  if k not in _HIDDEN and k not in covered]
    if _uncovered and current_group is not None:
        form.addRow(_section_header("Other"))
    for key, value in _uncovered:
        label = key.replace("_", " ").title() + ":"
        if isinstance(value, bool):
            widget = _make_checkbox(
                "", value, lambda c, k=key: editor_self.update_object_prop(k, c))
        elif isinstance(value, int):
            widget = _make_spin(value, -99999, 99999)
            widget.editingFinished.connect(
                lambda w=widget, k=key: editor_self.update_object_prop(k, w.value()))
        elif isinstance(value, float):
            widget = QLineEdit(str(value))
            widget.editingFinished.connect(
                lambda le=widget, k=key: editor_self.update_object_prop(k, _to_float(le.text())))
        else:
            widget = QLineEdit("" if value is None else str(value))
            widget.editingFinished.connect(
                lambda le=widget, k=key: editor_self.update_object_prop(k, le.text()))
        form.addRow(label, widget)
