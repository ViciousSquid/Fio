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
