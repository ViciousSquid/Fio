"""What HD_High needs of the shared infrastructure, added on its own side.

``engine.renderer.core`` is shared with the classic renderer and is left as
it is. Two things there fall short of what a physically shaded frame needs;
both are extended here, without touching the shared code:

* a model's draw groups carry no roughness, metalness or MTL emission
  (:class:`HDModelDrawGroup`, :func:`model_draw_groups`);
* the terrain's own programs (the block mesh's, the grass's) are compiled
  inside :class:`~engine.renderer.core.terrain_gl.TerrainGL`, where an HD
  renderer must have its say in the fragment sources (:class:`HDTerrainGL`).
"""

from contextlib import contextmanager

from ..core import terrain_gl as _terrain_gl
from ..core.resources import ModelDrawGroup
from ..core.terrain_gl import TerrainGL


class HDModelDrawGroup(ModelDrawGroup):
    """A model draw group with what an HD frame shades it with."""

    __slots__ = ('surface',)

    def __init__(self, *args, surface=None):
        super().__init__(*args)
        #: ``(roughness, metalness)`` where the material gives them (a GLB's
        #: PBR factors, an MTL's ``Pr`` / ``Pm``); None takes the
        #: environment's.
        self.surface = surface


def model_draw_groups(renderer, obj):
    """*obj*'s draw groups for HD_High, resolved once per model object: as
    ``ResourcesMixin._model_draw_groups`` resolves them, with each group's
    surface values, and an MTL's ``Ke`` as its emission (the classic
    renderer has never taken emission from an MTL, so obj_loader keeps it
    under ``hd_emissive``, a key only this reads)."""
    entry = renderer._model_draw_group_cache.get(id(obj))
    if entry is not None and entry[0] is obj:
        return entry[1]
    materials = obj.materials or {}
    has_ebo = getattr(obj, 'ebo', None) is not None
    resolved = []
    for group in obj.groups or ():
        material = materials.get(
            group['material'], {'color': [0.8, 0.8, 0.8], 'texture': None})
        texture = material.get('texture')
        colour = material.get('color', [0.8, 0.8, 0.8])
        emissive = material.get('hd_emissive', material.get('emissive', (0.0, 0.0, 0.0)))
        surface = None
        if 'roughness' in material or 'metallic' in material:
            try:
                surface = (float(material.get('roughness', 0.8)),
                           float(material.get('metallic', 0.0)))
            except (TypeError, ValueError):
                surface = None
        resolved.append(HDModelDrawGroup(
            int(group['start']), int(group['count']),
            bool(group.get('indexed', False)) and has_ebo,
            float(material.get('alpha', 1.0)),
            (float(colour[0]), float(colour[1]), float(colour[2])),
            bool(texture),
            int(renderer._model_texture_id(texture, material, manual=False))
            if texture else 0,
            (float(emissive[0]), float(emissive[1]), float(emissive[2])),
            surface=surface))
    # Ink and edge geometry must establish depth before the acrylic
    # surfaces (a stable sort: authored order within each half).
    resolved.sort(key=lambda g: g.alpha < 1.0)
    groups = tuple(resolved)
    renderer._model_draw_group_cache[id(obj)] = (obj, groups)
    return groups


@contextmanager
def compiling(module, source, ready):
    """While this is open, the shaders *module* compiles with its own
    ``compileShader`` / ``compileProgram`` get HD_High's treatment: each
    fragment source goes through *source* first, each linked program is
    handed to *ready*. For code in the shared infrastructure that compiles a
    program itself; the module's names are put back on the way out."""
    import OpenGL.GL as gl
    compile_shader, compile_program = module.compileShader, module.compileProgram

    def shader(text, kind):
        if kind == gl.GL_FRAGMENT_SHADER:
            text = source(text)
        return compile_shader(text, kind)

    def program(*shaders, **options):
        linked = compile_program(*shaders, **options)
        if linked:
            ready(linked)
        return linked

    module.compileShader, module.compileProgram = shader, program
    try:
        yield
    finally:
        module.compileShader, module.compileProgram = compile_shader, compile_program


class HDTerrainGL(TerrainGL):
    """The shared terrain GL state, with the programs it compiles itself
    (the block mesh's and the grass's) built the HD way."""

    def __init__(self, source, ready):
        super().__init__()
        self._hd_source = source
        self._hd_ready = ready

    def _compile_program(self, vertex_name):
        with compiling(_terrain_gl, self._hd_source, self._hd_ready):
            return super()._compile_program(vertex_name)

    def _init_grass_shader(self):
        with compiling(_terrain_gl, self._hd_source, self._hd_ready):
            return super()._init_grass_shader()
