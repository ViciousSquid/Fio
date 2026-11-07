"""One material record per brush face: the rule, the reader and the writer.

Stage 1 of the unified brush model (``docs/brush-model-proposal.md``). Every
face of every brush has exactly one authoritative material record:

* a **box** brush has six tagged faces (``south north west east down top``)
  whose material lives in the brush's per-tag dicts -- ``textures``,
  ``uv_scale``, ``uv_shift``, ``uv_angle``, ``uv_natural`` -- as it always
  has. No plane dicts, nothing new: the box encoding and its fast paths are
  untouched by this module;
* a **convex** brush has one face per plane, and the face's material lives on
  its plane -- ``texture``, ``uv_scale``, ``uv_shift``, ``uv_angle``,
  ``uv_natural`` -- for tagged and cut faces alike.

Brush-wide ``texture_tiling`` applies to both: a face with no scale of its
own and no Natural flag keeps a constant texel size.

**Legacy convex brushes.** Before Stage 1 a tagged convex face could carry
its material twice: on its plane (what the renderer drew) and in the per-tag
dicts (what the Surface Inspector showed and edited). Maps saved then (map
``version`` below :data:`CANONICAL_MAP_VERSION`) still hold such brushes.
They are migrated once, when the map is loaded (:func:`migrate_legacy`):
:data:`PRECEDENCE` decides, field by field, which value is the authoritative
one, writes it to the plane and drops the copy. After that there is one
value per field, and nothing at run time consults a per-tag dict for a
convex face -- a stray per-tag entry on a convex brush is ignored, never
promoted, so an obsolete legacy value cannot come back (a disabled Natural
flag stays disabled).

The rule follows from which code wrote each home before Stage 1:

* ``texture`` -- **the plane**. Every editor path that textures a tagged
  convex face (the Surface Inspector, the asset browser's apply-to-face)
  wrote the plane, and the convex renderer has always drawn the plane's
  texture. The only writers of a per-tag texture alone on a convex brush
  were tools that judge a face by the brush's AABB -- autocaulk and the
  trigger toggle -- whose values never rendered and are not reliable for a
  sloped face.
* ``uv_scale``, ``uv_shift``, ``uv_angle`` -- **the per-tag entry when there
  is one, else the plane**. The Surface Inspector, Fit, Natural and the
  Page Up/Down rotation wrote a tagged face's mapping to the per-tag dicts
  and the inspector displayed them; nothing in the editor wrote a tagged
  plane's mapping. A plane value on a tagged face is a copy made when the
  box was converted, or the only value there is (generated or hand-made
  brushes) -- so an authored per-tag value is the newer one, and a plane
  value stands where no per-tag value exists.
* ``uv_natural`` -- **either home**: a flag set in either place means
  Natural, as the Surface Inspector has always shown it.

A cut face (no tag) has only its plane. Per-tag entries for tags no plane
carries are left alone by the migration: they describe no face, and they
come back into use if the brush returns to a box.

GL-free and free of other engine imports for everything that works on a
plane dict; :func:`face_material` and friends import the geometry kernel
lazily to list a convex brush's faces.
"""

import collections
import math

#: The material fields of a face, in a fixed order.
FIELDS = ('texture', 'uv_scale', 'uv_shift', 'uv_angle', 'uv_natural')

#: The brush-level per-tag dict that holds each field for a tagged face.
TAG_DICTS = {'texture': 'textures', 'uv_scale': 'uv_scale', 'uv_shift': 'uv_shift',
             'uv_angle': 'uv_angle', 'uv_natural': 'uv_natural'}

#: Box face tags, in the cube VAO's order.
FACE_TAGS = ('south', 'north', 'west', 'east', 'down', 'top')

PLANE, TAG, EITHER = 'plane', 'tag', 'either'

#: The map ``version`` from which brushes are saved in the canonical layout
#: (one record per face; an angled face's on its plane only). Earlier
#: versions are migrated at load by :func:`migrate_legacy`.
CANONICAL_MAP_VERSION = 4

#: Which home is authoritative for a legacy tagged convex face, per field,
#: when :func:`migrate_legacy` settles it (see the module docstring for why).
PRECEDENCE = {'texture': PLANE, 'uv_scale': TAG, 'uv_shift': TAG,
              'uv_angle': TAG, 'uv_natural': EITHER}

DEFAULT_TEXTURE = 'default.png'

#: Texture names that do not make a brush "textured" (it is then drawn in the
#: brush colour by the solid pass).
UNTEXTURED = (DEFAULT_TEXTURE, 'caulk.jpg')

#: A face's resolved material: what it is drawn with. ``uv_scale`` is
#: ``None`` for "fit the face" (no scale of its own); ``natural`` is the
#: constant-texel-size mode, brush-wide tiling included.
Material = collections.namedtuple(
    'Material', ('texture', 'uv_scale', 'uv_shift', 'uv_angle', 'natural'))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def _pair(value):
    try:
        u, v = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if not (math.isfinite(u) and math.isfinite(v)):
        return None
    return (u, v)


def _value(field, raw):
    """*raw* as the field's value, or ``None`` for "no value here"."""
    if raw is None:
        return None
    if field == 'texture':
        return raw if isinstance(raw, str) and raw else None
    if field in ('uv_scale', 'uv_shift'):
        return _pair(raw)
    if field == 'uv_angle':
        try:
            angle = float(raw)
        except (TypeError, ValueError):
            return None
        return angle if math.isfinite(angle) else None
    return True if raw else None            # uv_natural


def _stored(field, value):
    """*value* as it is written to a dict (JSON-friendly)."""
    if field in ('uv_scale', 'uv_shift'):
        return [float(value[0]), float(value[1])]
    if field == 'uv_angle':
        return float(value)
    return value


def tag_value(brush, field, tag):
    """The value a per-tag dict holds for *tag*, or ``None``."""
    if not tag:
        return None
    entries = brush.get(TAG_DICTS[field])
    if not isinstance(entries, dict):
        return None
    return _value(field, entries.get(tag))


def plane_value(plane, field):
    """The value a plane holds for *field*, or ``None``."""
    return _value(field, plane.get(field))


def choose(field, plane_value_, tag_value_):
    """:data:`PRECEDENCE` for one field of a legacy face: the authoritative
    value or ``None``. Used by the load-time migration only."""
    rule = PRECEDENCE[field]
    if rule == PLANE:
        return plane_value_
    if rule == TAG:
        return tag_value_ if tag_value_ is not None else plane_value_
    return True if (plane_value_ or tag_value_) else None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def has_geometry(brush):
    geometry = brush.get('geometry')
    return bool(isinstance(geometry, dict) and geometry.get('planes'))


def plane_record(brush, plane):
    """A convex face's record, ``{field: value}``: its plane's values, the
    one home of an angled face's material. (*brush* is accepted for symmetry
    with :func:`box_record`; a per-tag dict is never consulted here.)"""
    out = {}
    for field in FIELDS:
        value = plane_value(plane, field)
        if value is not None:
            out[field] = value
    return out


def legacy_record(brush, plane):
    """A legacy tagged face's authoritative record: its plane and the
    brush's per-tag dicts settled by :data:`PRECEDENCE`."""
    tag = plane.get('face')
    out = {}
    for field in FIELDS:
        value = choose(field, plane_value(plane, field), tag_value(brush, field, tag))
        if value is not None:
            out[field] = value
    return out


def box_record(brush, tag):
    """A box face's authoritative record: its per-tag entries."""
    out = {}
    for field in FIELDS:
        value = tag_value(brush, field, tag)
        if value is not None:
            out[field] = value
    return out


def resolve(record, tiling=False):
    """The :class:`Material` a face with *record* is drawn with."""
    scale = record.get('uv_scale')
    return Material(
        record.get('texture') or DEFAULT_TEXTURE,
        scale,
        record.get('uv_shift') or (0.0, 0.0),
        record.get('uv_angle') or 0.0,
        bool(record.get('uv_natural')) or (bool(tiling) and scale is None))


def plane_material(brush, plane):
    """The :class:`Material` a convex face on *plane* is drawn with."""
    return resolve(plane_record(brush, plane), brush.get('texture_tiling', False))


def convex_materials(convex, tiling=False):
    """``{id(face): Material}`` for every face of a solved convex shape,
    resolved from the shape's own copies of its planes. Cached on the shape
    (which is rebuilt whenever a plane -- material included -- changes), so
    a table sync re-resolves nothing for an unchanged brush."""
    tiling = bool(tiling)
    cached = convex._material_cache
    if cached is not None and cached[0] == tiling:
        return cached[1]
    materials = {id(face): resolve(plane_record(None, convex.planes[face['plane']]), tiling)
                 for face in convex.faces}
    convex._material_cache = (tiling, materials)
    return materials


def box_material(brush, tag):
    """The :class:`Material` a box face is drawn with."""
    return resolve(box_record(brush, tag), brush.get('texture_tiling', False))


def face_textures(brush):
    """The texture of every face, by the authoritative record: a box's six
    per-tag entries, a convex brush's planes (tagged and cut)."""
    if has_geometry(brush):
        return [plane_value(plane, 'texture') or DEFAULT_TEXTURE
                for plane in brush['geometry']['planes']]
    textures = brush.get('textures')
    if not isinstance(textures, dict):
        return []
    return [name for name in textures.values() if isinstance(name, str) and name]


def is_textured(brush):
    """Whether any face names a real texture (the textured pass draws it;
    otherwise the solid pass draws every face in the brush colour)."""
    return any(name not in UNTEXTURED for name in face_textures(brush))


def tag_textures(brush):
    """``{tag: texture}`` for the tagged faces of any brush."""
    return box_material_dicts(brush).get('textures', {})


def box_material_dicts(brush):
    """The five per-tag dicts (``textures``, ``uv_scale`` ...) describing a
    brush's tagged faces, for any brush: a copy of a box's own, or built from
    a convex brush's tagged planes. What box-making operations (CSG
    fragments, hollow) take a brush's look from, every field included."""
    out = {}
    for field in FIELDS:
        key = TAG_DICTS[field]
        entries = {}
        if has_geometry(brush):
            for plane in brush['geometry']['planes']:
                tag = plane.get('face')
                value = plane_value(plane, field) if tag in FACE_TAGS else None
                if value is not None:
                    entries[tag] = _stored(field, value)
        else:
            for tag in FACE_TAGS:
                value = tag_value(brush, field, tag)
                if value is not None:
                    entries[tag] = _stored(field, value)
        if entries:
            out[key] = entries
    return out


def _convex_faces(brush):
    from engine import brush_geometry as bg
    convex = bg.get_convex(brush)
    if convex is None or not convex.is_valid:
        return []
    return [(bg.face_key(face), face) for face in convex.faces]


def face_keys(brush):
    """Every face of *brush*: box tags, or convex face keys (a tag, or
    ``'#<plane index>'`` for a cut face)."""
    if has_geometry(brush):
        return [key for key, _face in _convex_faces(brush)]
    return list(FACE_TAGS)


def _plane_for_key(brush, key):
    """``(index, plane)`` backing a convex face key, or ``(None, None)``."""
    planes = brush['geometry']['planes']
    if isinstance(key, str) and key.startswith('#'):
        try:
            index = int(key[1:])
        except ValueError:
            return None, None
        return (index, planes[index]) if 0 <= index < len(planes) else (None, None)
    for index, plane in enumerate(planes):
        if plane.get('face') == key:
            return index, plane
    return None, None


def face_record(brush, key):
    """The authoritative record of one face (what the Surface Inspector
    shows): ``{field: value}`` for the fields that have one."""
    if has_geometry(brush):
        _index, plane = _plane_for_key(brush, key)
        return plane_record(brush, plane) if plane is not None else {}
    return box_record(brush, key)


def face_material(brush, key):
    """The :class:`Material` one face is drawn with."""
    tiling = brush.get('texture_tiling', False)
    return resolve(face_record(brush, key), tiling)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

_UNSET = object()


def _write(target, field, value):
    if value is None:
        target.pop(field, None)
    else:
        target[field] = _stored(field, value)


def _drop_tag_entry(brush, field, tag):
    entries = brush.get(TAG_DICTS[field])
    if isinstance(entries, dict) and tag in entries:
        entries = dict(entries)
        del entries[tag]
        if entries:
            brush[TAG_DICTS[field]] = entries
        else:
            brush.pop(TAG_DICTS[field], None)


def set_face_material(brush, key, texture=_UNSET, uv_scale=_UNSET, uv_shift=_UNSET,
                      uv_angle=_UNSET, uv_natural=_UNSET):
    """Write any subset of one face's material to its one home.

    ``None`` clears a field (``uv_natural=False`` too). For a convex face the
    plane is written and any legacy per-tag copy of that field is removed,
    so the face is left with one record; the brush's derived geometry is
    invalidated (a GPU mesh bakes its faces' material). Returns ``False``
    when *key* names no face.
    """
    given = {'texture': texture, 'uv_scale': uv_scale, 'uv_shift': uv_shift,
             'uv_angle': uv_angle, 'uv_natural': uv_natural}
    values = {field: _value(field, value) for field, value in given.items()
              if value is not _UNSET}
    if has_geometry(brush):
        index, plane = _plane_for_key(brush, key)
        if plane is None:
            return False
        tag = plane.get('face')
        for field, value in values.items():
            _write(plane, field, value)
            if tag:
                _drop_tag_entry(brush, field, tag)
        from engine import brush_geometry as bg
        bg.invalidate_geometry_cache(brush)
        return True
    if key not in FACE_TAGS:
        return False
    for field, value in values.items():
        entries = brush.get(TAG_DICTS[field])
        entries = dict(entries) if isinstance(entries, dict) else {}
        if value is None:
            entries.pop(key, None)
        else:
            entries[key] = _stored(field, value)
        if entries:
            brush[TAG_DICTS[field]] = entries
        else:
            brush.pop(TAG_DICTS[field], None)
    return True


# ---------------------------------------------------------------------------
# Conversion and normalisation
# ---------------------------------------------------------------------------

Resolution = collections.namedtuple(
    'Resolution', ('tag', 'field', 'kept', 'plane', 'per_tag'))


def _tags_on_planes(brush):
    return {plane.get('face') for plane in brush['geometry']['planes'] if plane.get('face')}


def _has_tag_copies(brush, tags):
    return any(isinstance(brush.get(TAG_DICTS[f]), dict)
               and tags & set(brush[TAG_DICTS[f]]) for f in FIELDS)


def _drop_tag_entries(brush, tags):
    for field in FIELDS:
        entries = brush.get(TAG_DICTS[field])
        if not isinstance(entries, dict):
            continue
        kept_entries = {k: v for k, v in entries.items() if k not in tags}
        if kept_entries:
            brush[TAG_DICTS[field]] = kept_entries
        else:
            brush.pop(TAG_DICTS[field], None)


def migrate_legacy(brush):
    """Give every tagged face of a legacy convex brush one material record.

    Run once, when a map saved before the canonical layout is loaded. The
    authoritative value of each field (:data:`PRECEDENCE`) is written to the
    plane and the per-tag copies of tags that a plane carries are removed.
    Planes and per-tag dicts are replaced, never edited in place, so a brush
    shallow-copied from a document leaves the document as it was. Boxes are
    returned untouched. Returns the :class:`Resolution` of every field whose
    two homes disagreed (``kept`` is ``'plane'`` or ``'tag'``).
    """
    if not has_geometry(brush):
        return []
    tags = _tags_on_planes(brush)
    if not _has_tag_copies(brush, tags):
        return []
    resolutions = []
    new_planes = []
    for plane in brush['geometry']['planes']:
        tag = plane.get('face')
        if not tag:
            new_planes.append(plane)
            continue
        plane = dict(plane)
        for field in FIELDS:
            on_plane = plane_value(plane, field)
            per_tag = tag_value(brush, field, tag)
            kept = choose(field, on_plane, per_tag)
            if per_tag is not None and per_tag != on_plane:
                resolutions.append(Resolution(
                    tag, field, 'plane' if kept == on_plane else 'tag',
                    on_plane, per_tag))
            _write(plane, field, kept)
        new_planes.append(plane)
    geometry = dict(brush['geometry'])
    geometry['planes'] = new_planes
    brush['geometry'] = geometry
    _drop_tag_entries(brush, tags)
    from engine import brush_geometry as bg
    bg.invalidate_geometry_cache(brush)
    return resolutions


def drop_tag_copies(brush):
    """Remove per-tag entries for the tags a convex brush's planes carry.

    For a brush already in the canonical layout -- loaded from a map of
    :data:`CANONICAL_MAP_VERSION` or later, or just converted from a box --
    the plane is the only record, so such an entry is a stray copy and is
    dropped without being consulted. Returns the number of entries dropped.
    """
    if not has_geometry(brush):
        return 0
    tags = _tags_on_planes(brush)
    if not _has_tag_copies(brush, tags):
        return 0
    dropped = sum(len(tags & set(brush[TAG_DICTS[f]])) for f in FIELDS
                  if isinstance(brush.get(TAG_DICTS[f]), dict))
    _drop_tag_entries(brush, tags)
    return dropped


def legacy_layout(brush):
    """A copy of a canonical brush laid out as a build before Stage 1 reads it.

    Such a build draws an angled face's texture, scale, shift and angle from
    its plane, but takes a tagged face's Natural flag and the brush's
    textured-pass routing from the per-tag dicts, and its Surface Inspector
    reads them too -- so every tagged face's record is copied into them.
    Returns ``(brush, lossy)``: *lossy* lists the faces whose appearance that
    layout cannot express (``'#i.uv_natural'`` for a Natural cut face,
    ``'tiling'`` for an angled brush with brush-wide tiling); boxes are
    returned as they are. For :func:`export_legacy_level` only -- never for a
    normal save.
    """
    import copy
    out = copy.deepcopy(brush)
    if not has_geometry(out):
        return out, []
    lossy = []
    for field, entries in box_material_dicts(out).items():
        merged = dict(out.get(field) or {}) if isinstance(out.get(field), dict) else {}
        merged.update(entries)
        out[field] = merged
    for index, plane in enumerate(out['geometry']['planes']):
        if not plane.get('face') and plane.get('uv_natural'):
            lossy.append('#%d.uv_natural' % index)
    if out.get('texture_tiling'):
        lossy.append('tiling')
    return out, lossy


def export_legacy_level(level):
    """A copy of a canonical level document for builds before Stage 1:
    every brush through :func:`legacy_layout`, ``version`` set back to 3.
    Returns ``(level, lossy)`` with *lossy* as ``{brush id: [face, ...]}``.

    A version-5 level (an angled brush's planes in its local frame) is first
    laid out as version 4 (``engine.brush_placement.export_world_level``):
    every build before Stage 2d reads world-space planes."""
    import copy
    try:
        version = int(level.get('version') or 0)
    except (TypeError, ValueError):
        version = 0
    if version >= 5:
        from engine.brush_placement import export_world_level
        level = export_world_level(level)
    out = copy.deepcopy({k: v for k, v in level.items() if k != 'brushes'})
    out['version'] = CANONICAL_MAP_VERSION - 1
    out['brushes'] = []
    lossy = {}
    for brush in level.get('brushes') or []:
        converted, faces = legacy_layout(brush)
        out['brushes'].append(converted)
        if faces:
            lossy[str(brush.get('id'))] = faces
    return out, lossy


def tag_material_onto_planes(brush, planes):
    """Write a box's per-tag material onto the tagged *planes* made from it
    (all five fields), as converting it to convex must."""
    for plane in planes:
        tag = plane.get('face')
        if tag:
            for field, value in box_record(brush, tag).items():
                _write(plane, field, value)
    return planes


def planes_onto_tags(brush):
    """Move the material of a convex brush's tagged planes into its per-tag
    dicts, as returning it to a box must. Cut faces have no box side to go
    to; their material goes with the planes."""
    if not has_geometry(brush):
        return
    for plane in brush['geometry']['planes']:
        tag = plane.get('face')
        if not tag or tag not in FACE_TAGS:
            continue
        record = plane_record(brush, plane)
        for field in FIELDS:
            entries = brush.get(TAG_DICTS[field])
            entries = dict(entries) if isinstance(entries, dict) else {}
            if field in record:
                entries[tag] = _stored(field, record[field])
            else:
                entries.pop(tag, None)
            if entries:
                brush[TAG_DICTS[field]] = entries
            else:
                brush.pop(TAG_DICTS[field], None)
