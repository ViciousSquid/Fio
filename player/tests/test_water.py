"""Water classification and meshes must not expose editor trigger materials."""
import numpy as np
import pytest

from player.render.scene import build_textured_batches, spatial_batches
from player.render.water import water_mesh


@pytest.mark.parametrize('material', [dict(shader='Water'), dict(is_water=True),
                                     dict(textures={'top':'water_blue.png'})])
def test_water_never_enters_opaque_or_shadow_batches(material):
    water = dict(pos=[0,0,0],size=[100,20,100],textures={'top':'trigger.jpg'})
    water.update(material)
    solid = dict(pos=[200,0,0],size=[10,10,10])
    assert build_textured_batches({'brushes':[water]}) == {}
    batches = list(spatial_batches([water,solid],512))
    assert sum(v.size//8 for groups,_ in batches for v in groups.values()) == 36


def test_water_plane_has_only_upward_faces_and_real_interior_wave_vertices():
    vertices = water_mesh(dict(pos=[0,0,0],size=[100,20,100],water_plane=True))
    assert np.all(vertices[:,1] == .5)
    assert np.all(vertices[:,3:6] == [0,1,0])
    assert np.any(np.all(np.abs(vertices[:,[0,2]]) < .4,axis=1))
    triangles = vertices.reshape(-1,3,8)
    normals = np.cross(triangles[:,1,:3]-triangles[:,0,:3],triangles[:,2,:3]-triangles[:,0,:3])
    assert np.all(normals[:,1] > 0)


def test_convex_water_preserves_clipped_shape_and_ignores_trigger_material():
    from engine.brush_geometry import box_to_geometry, get_convex, make_plane
    brush = dict(pos=[20,5,40],size=[100,20,80],textures={'top':'trigger.jpg'})
    box_to_geometry(brush)
    brush['geometry']['planes'].append(make_plane([1,0,1],[20,0,40],texture='trigger.jpg'))
    convex = get_convex(brush)
    vertices = water_mesh(brush)
    points = vertices[:,:3]*brush['size']+brush['pos']
    assert len(vertices) > 0
    assert np.all(points[:,0]+points[:,2] <= 60.001)
    for point in points:
        assert np.min(np.linalg.norm(convex.verts-point,axis=1)) < .001
