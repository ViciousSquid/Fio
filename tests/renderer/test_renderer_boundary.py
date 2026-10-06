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


def test_outside_code_imports_only_the_public_package():
    outside = [p for top in ("engine", "editor", "plugins", "player") for p in (ROOT / top).rglob("*.py")
               if PACKAGE not in p.parents and "tests" not in p.parts]
    internal = ("engine.renderer.core", "engine.renderer.forward",
                "engine.renderer.api", "engine.renderer.registry")
    assert not _offenders(outside, internal)


def test_world_logic_and_editor_state_do_not_import_the_renderer():
    layers = [ROOT / "engine" / name for name in (
        "render_table.py", "entity_table.py", "terrain_table.py", "logic_thread.py",
        "logic_render.py", "threaded_game_state.py")] + [ROOT / "editor" / "editor_state.py"]
    assert not _offenders(layers, ("engine.renderer",))


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
    tree = ast.parse((ROOT / "engine" / "qt_game_view.py").read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "renderer"
            and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"}
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
