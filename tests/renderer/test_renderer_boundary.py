"""The renderer plugin boundary, enforced on the import graph and the contract.

Target architecture::

    EditorState -> LogicThread -> RenderTable / EntityTable -> Renderer API -> active renderer

These read source only (no GL, no Qt), so they run in the fast headless tier:

* ``engine.renderer`` imports nothing from ``editor``: it consumes
  renderer-facing data (the dense tables, ``SelectionOverlay``), never editor
  objects.
* The public surface (``api``, ``registry``, the package ``__init__``) imports
  no OpenGL/Qt/glm and no implementation, so a renderer can be written and
  registered against it alone.
* ``core`` never reaches into ``forward``: it is infrastructure any renderer
  may reuse, not part of the built-in one.
* Outside the package, code imports only ``engine.renderer`` itself -- never
  an implementation module.
* The world, logic and editor-state layers do not import the renderer at all.
* The ``Renderer`` protocol carries no forward-renderer internals, and every
  frame-input key the built-in renderer reads is part of the documented frame
  input.
"""

import ast
import importlib.util
import pathlib
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "engine" / "renderer"
GL_MODULES = ("OpenGL", "PyQt5", "glm")


def _module_name(path):
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path):
    """Absolute names of every module *path* imports (relative ones resolved)."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join(anchor + ([node.module] if node.module else []))
            names.append(base)
            # ``from pkg import submodule`` imports the submodule too; a
            # non-module name only adds an entry no forbidden prefix matches.
            names += [f"{base}.{alias.name}" for alias in node.names]
    return names


def _package_files():
    return sorted(PACKAGE.rglob("*.py"))


def _offenders(files, forbidden):
    return sorted({f"{p.relative_to(ROOT)} imports {name}"
                   for p in files for name in _imports(p)
                   if any(name == f or name.startswith(f + ".") for f in forbidden)})


def test_the_renderer_imports_nothing_from_the_editor():
    assert not _offenders(_package_files(), ("editor",))


def test_the_public_surface_needs_no_gl_and_no_implementation():
    public = [PACKAGE / "__init__.py", PACKAGE / "api.py", PACKAGE / "registry.py"]
    assert not _offenders(public, GL_MODULES + ("engine.renderer.core", "engine.renderer.forward"))


def test_importing_the_public_package_loads_no_gl():
    code = textwrap.dedent(f"""
        import sys
        import engine.renderer
        loaded = sorted(m for m in sys.modules if m.split('.')[0] in {GL_MODULES!r})
        assert not loaded, loaded
    """)
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def test_core_never_reaches_into_the_forward_renderer():
    assert not _offenders(sorted((PACKAGE / "core").rglob("*.py")), ("engine.renderer.forward",))


#: A renderer implementation that lives outside the package: the standalone
#: player's GLES renderer. A renderer may reuse ``core`` (plugins/API.md:
#: "engine.renderer.core.terrain_gl.TerrainGL is the built-in implementation,
#: reusable as it stands"); it still may not reach the forward renderer or
#: the package's private modules.
OUTSIDE_RENDERERS = (ROOT / "player" / "render",)


def test_outside_code_imports_only_the_public_package():
    outside = [p for top in ("engine", "editor", "plugins", "player") for p in (ROOT / top).rglob("*.py")
               if PACKAGE not in p.parents and "tests" not in p.parts]
    private = ("engine.renderer.forward", "engine.renderer.api", "engine.renderer.registry")
    renderers = [p for p in outside if any(root in p.parents for root in OUTSIDE_RENDERERS)]
    hosts = [p for p in outside if p not in renderers]
    assert not _offenders(hosts, private + ("engine.renderer.core",))
    assert not _offenders(renderers, private)


def test_world_logic_and_editor_state_do_not_import_the_renderer():
    layers = [ROOT / "engine" / name for name in (
        "render_table.py", "entity_table.py", "terrain_table.py", "logic_thread.py",
        "logic_render.py", "threaded_game_state.py")] + [ROOT / "editor" / "editor_state.py"]
    assert not _offenders(layers, ("engine.renderer",))


#: The viewport's delegated responsibilities, each a class holding the view.
HOST_PARTS = ("view_audio", "view_components", "view_debug_draw", "view_hud", "view_messages", "view_pause", "view_picking", "view_terrain_edit")


def _host_files():
    return [ROOT / "engine" / "qt_game_view.py"] + [
        ROOT / "engine" / ("%s.py" % name) for name in HOST_PARTS]


def _protocol_members():
    tree = ast.parse((PACKAGE / "api.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Renderer")
    names = set()
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign):
            names.add(node.target.id)
    return names


def test_the_protocol_exposes_no_forward_renderer_internals():
    leaked = _protocol_members() & {
        "shaders", "uniforms", "vaos", "lod_manager", "render_shadow_maps",
        "draw_models_instanced", "draw_sprites_instanced", "draw_billboards_instanced",
        "update_grid_buffers", "setup_terrain_shader", "prepare_terrain",
        "preload_level_textures", "lowpower_mode", "WATER_QUALITIES",
    }
    assert not leaked
    assert not any(name.startswith("_") for name in _protocol_members())


def test_the_host_uses_only_the_protocol():
    """Every ``self.renderer.<name>`` the viewport touches is a contract member."""
    members = _protocol_members()
    used = set()
    # The viewport and the classes it delegates to (engine/view_*.py), which
    # reach the renderer as ``self.view.renderer``.
    for path in _host_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "renderer"):
                continue
            owner = node.value.value
            if (isinstance(owner, ast.Name) and owner.id == "self") or (
                    isinstance(owner, ast.Attribute) and owner.attr == "view"
                    and isinstance(owner.value, ast.Name) and owner.value.id == "self"):
                used.add(node.attr)
    assert used and used <= members, sorted(used - members)


def test_the_built_in_renderer_reads_only_the_documented_frame_input():
    sys.path.insert(0, str(ROOT))
    try:
        spec = importlib.util.spec_from_file_location("_fio_renderer_api", PACKAGE / "api.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
    finally:
        sys.path.remove(str(ROOT))
    read = set()
    for path in _package_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            target = None
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("config", "cfg") and node.args
                    and isinstance(node.args[0], ast.Constant)):
                target = node.args[0].value
            elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id in ("config", "cfg") and isinstance(node.slice, ast.Constant)):
                target = node.slice.value
            if isinstance(target, str):
                read.add(target)
    assert read and read <= set(api.FRAME_INPUT), sorted(read - set(api.FRAME_INPUT))


# -- tables all the way to GL ------------------------------------------------
#
# A frame reads the published dense tables and the renderer's own caches.
# Authored-shaped objects -- a loaded model's group and material dictionaries,
# a geometry record's convex solid and face materials, brush dictionaries,
# Things -- may be read only where a GPU resource is built on a cache miss.
# Those places are listed here; the list is to shrink, not to grow.

AUTHORED_ATTRIBUTES = {
    "materials", "groups", "convex", "geometry_records",
    "brushes", "things", "properties", "editor", "mtl_dir",
}
RESOURCE_BUILDERS = {
    # A model's draw groups, resolved once per loaded model.
    ("core/resources.py", "_model_draw_groups"): {"materials", "groups"},
    # A convex brush's mesh, built when its geometry handle is first resolved
    # (only when the table's geometry_version has moved).
    ("core/geometry.py", "tessellate_geo"): {"convex"},
    ("core/geometry.py", "_get_geo_mesh_record"): {"convex"},
    ("core/geometry.py", "_resolve_face_run"): {"materials"},
    ("core/geometry.py", "_sync_geo_handles"): {"geometry_records"},
}
# Dictionary keys of the authored formats (brush, material). A subscript or
# ``.get`` with one of these names is a read of such a dictionary.
AUTHORED_KEYS = {"mtl_dir", "material", "textures", "is_trigger", "is_mover"}
AUTHORED_KEY_READERS = {
    ("core/resources.py", "_model_draw_groups"),      # group['material']
    ("core/resources.py", "_resolve_model_texture_path"),   # on a texture miss
}


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _own_nodes(function):
    """The nodes of *function* itself, nested functions' bodies included."""
    return ast.walk(function)


def test_authored_objects_are_read_only_where_a_gpu_resource_is_built():
    found = {}
    for path in _package_files():
        relative = path.relative_to(PACKAGE).as_posix()
        for function in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            for node in _own_nodes(function):
                if isinstance(node, ast.Attribute) and node.attr in AUTHORED_ATTRIBUTES:
                    found.setdefault((relative, function.name), set()).add(node.attr)
    unexpected = {where: sorted(names) for where, names in found.items()
                  if not names <= RESOURCE_BUILDERS.get(where, set())}
    assert not unexpected, unexpected
    stale = sorted(set(RESOURCE_BUILDERS) - set(found))
    assert not stale, "no longer reads authored objects; drop from the list: %s" % stale


def test_authored_dictionary_keys_are_read_only_on_a_resource_miss():
    found = set()
    for path in _package_files():
        relative = path.relative_to(PACKAGE).as_posix()
        for function in _functions(ast.parse(path.read_text(encoding="utf-8"))):
            for node in _own_nodes(function):
                key = None
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                    key = node.slice.value
                elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                      and node.func.attr == "get" and node.args
                      and isinstance(node.args[0], ast.Constant)):
                    key = node.args[0].value
                if key in AUTHORED_KEYS:
                    found.add((relative, function.name))
    assert found <= AUTHORED_KEY_READERS, sorted(found - AUTHORED_KEY_READERS)


def test_the_model_passes_read_no_material_dictionary():
    """The forward renderer draws models from resolved draw groups alone."""
    source = (PACKAGE / "forward" / "passes.py").read_text(encoding="utf-8")
    for needle in ("obj.materials", "obj.groups", "material.get(", "group['", "group.get("):
        assert needle not in source, needle


def test_the_host_hands_the_renderer_no_authored_object_arrays():
    """``render_refs``/``entity_refs`` (the brush dictionaries and Things
    behind the tables' rows) stay with the host: they are not frame input."""
    source = (ROOT / "engine" / "qt_game_view.py").read_text(encoding="utf-8")
    for key in ("render_refs", "entity_refs"):
        assert '_render_config["%s"]' % key not in source
        assert "_render_config['%s']" % key not in source
    package = "".join(p.read_text(encoding="utf-8") for p in _package_files())
    assert "render_refs" not in package and "entity_refs]" not in package


def test_the_renderer_never_holds_the_terrain_object():
    """M5: the terrain frame input is a published ``TerrainFrame``. The
    package imports no ``engine.terrain`` (only the GL-free publication and
    table modules), calls nothing the Terrain object had, and writes no
    attribute on what it is handed."""
    assert not _offenders(_package_files(), ("engine.terrain",))
    source = "".join(p.read_text(encoding="utf-8") for p in _package_files())
    for needle in ("update_and_render(", ".advance(", ".publish(", "get_tri_count",
                   "release_renderer_resources"):
        assert needle not in source, needle
    passes = (PACKAGE / "forward" / "passes.py").read_text(encoding="utf-8")
    body = passes[passes.index("def render_terrain("):passes.index("def _terrain_gl(")]
    tree = ast.parse(textwrap.dedent(body))
    written = [ast.unparse(target) for node in ast.walk(tree)
               if isinstance(node, (ast.Assign, ast.AugAssign))
               for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
               if ast.unparse(target).startswith("terrain.")]
    assert not written, written


def test_the_terrain_and_its_publication_need_no_gl():
    for name in ("terrain.py", "terrain_frame.py", "terrain_table.py"):
        assert not _offenders([ROOT / "engine" / name], ("OpenGL",))


def test_the_host_publishes_the_terrain_instead_of_passing_it():
    source = (ROOT / "engine" / "qt_game_view.py").read_text(encoding="utf-8")
    assert 'self._render_config["terrain"] = terrain.publish()' in source
    assert "release_renderer_resources" not in source


def test_the_overlays_take_arrays_not_authored_objects():
    """The face highlight, the I/O links and the collision wireframes are
    drawn from arrays the host builds (engine/overlay_geometry.py): the
    renderer reads no brush or record key and imports no brush geometry."""
    path = PACKAGE / "core" / "overlays.py"
    source = path.read_text(encoding="utf-8")
    assert not _offenders([path], ("engine.brush_geometry",))
    tree = ast.parse(source)
    wanted = {"draw_face_highlight", "draw_connection_lines",
              "draw_collision_visualization"}
    found = {}
    for function in _functions(tree):
        if function.name not in wanted:
            continue
        found[function.name] = [a.arg for a in function.args.args]
        for node in ast.walk(function):
            key = None
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                key = node.slice.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "get" and node.args
                  and isinstance(node.args[0], ast.Constant)):
                key = node.args[0].value
            assert not (isinstance(key, str) and key in {
                "pos", "size", "src", "dst", "_model_collision",
                "_collision_mode", "_mesh_triangles"}), (function.name, key)
    assert found == {
        "draw_face_highlight": ["self", "projection", "view", "triangles"],
        "draw_connection_lines": ["self", "projection", "view", "segments", "colours"],
        "draw_collision_visualization": ["self", "projection", "view", "vertices", "runs"],
    }


def test_the_renderer_writes_nothing_on_a_published_table():
    """Per-table state (the occlusion index, the geometry handles) is kept
    by the renderer under the table's identity; no attribute of a table is
    assigned anywhere in the package."""
    written = []
    for path in _package_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id in {"table", "etable", "entity_table",
                                                "render_table", "light_table"}):
                    written.append("%s:%d %s" % (path.relative_to(ROOT), node.lineno,
                                                 ast.unparse(target)))
    assert not written, written
