"""Regressions for visible/gameplay failures missed by the movement-only gate."""
import io
import math
import numpy as np
import pytest
from PIL import Image
from player.render.scene import build_textured_batches
from player.render.assets import Images
from player.render.projection import EntityProjection, lights, effect_image
from player.render.hud import paint
from player.world import PlayerWorld
from player.input.state import InputState, ACTION_FIRE


def level(things=()):
    return {'brushes': [{'pos':[0,-16,0], 'size':[1024,32,1024]}],
            'things':[{'type':'PlayerStart','pos':[0,2,0],'properties':{'angle':90}}, *things]}


def test_surface_inspector_rotation_scale_shift_and_fit():
    brush={'pos':[0,0,0],'size':[256,64,128], 'textures':{'south':'wall'},
           'uv_angle':{'south':90},'uv_scale':{'south':[2,3]},'uv_shift':{'south':[.25,-.5]}}
    vertices=build_textured_batches({'brushes':[brush]})['wall'].reshape(-1,8)
    # Desktop cube's base UV=(x/width+.5,y/height+.5); rotate about centre,
    # then multiply by the authored repeat factors and add the authored shift.
    base=vertices[:,:2]/[256,64]
    expected=(np.column_stack((-base[:,1],base[:,0]))+.5)*[2,3]+[.25,-.5]
    np.testing.assert_allclose(vertices[:,6:],expected,atol=1e-6)
    brush.pop('uv_angle');brush.pop('uv_scale');brush.pop('uv_shift')
    uv=build_textured_batches({'brushes':[brush]})['wall'].reshape(-1,8)[:,6:]
    np.testing.assert_allclose(uv.max(0),[1,1])  # Fit is the desktop default.
    brush['uv_natural']={'south':True}
    uv=build_textured_batches({'brushes':[brush]},texture_size=lambda _: (64,32))['wall'].reshape(-1,8)[:,6:]
    np.testing.assert_allclose(uv.max(0),[4,2])


def test_nodraw_and_caulk_do_not_cover_the_world():
    brush={'pos':[0,0,0],'size':[1,1,1], 'textures':{'south':'nodraw.jpg','north':'caulk.jpg'}}
    batches=build_textured_batches({'brushes':[brush]})
    assert 'nodraw.jpg' not in batches and 'caulk.jpg' not in batches
    assert batches[''].size//8==24


@pytest.mark.parametrize('texture', ['nodraw', 'nodraw.jpg', 'NODRAW.PNG',
                                   'assets/textures/nodraw.webp', r'textures\NoDraw.JPG'])
@pytest.mark.parametrize('convex', [False, True])
def test_nodraw_skips_only_the_marked_face_without_loading_its_texture(texture, convex):
    from engine.brush_geometry import box_to_geometry
    from player.render.scene import build_brush_mesh
    brush = {'pos':[0,0,0], 'size':[64,64,64], 'textures':{'south':texture, 'north':'nodraw_bricks.jpg'},
             'uv_natural':{'south':True, 'north':True}}
    hidden_normal = [0,0,-1]
    if convex:
        box_to_geometry(brush)
        plane = next(p for p in brush['geometry']['planes'] if p.get('texture') == texture)
        hidden_normal = plane['n']
        plane['uv_natural'] = True
    loaded = []
    batches = build_textured_batches({'brushes':[brush]}, texel=128,
                                     texture_size=lambda name: loaded.append(name) or (64,64))
    assert texture not in batches and texture not in loaded
    assert sum(vertices.size//8 for vertices in batches.values()) == 30
    assert batches['nodraw_bricks.jpg'].size//8 == 6
    normals = np.concatenate(list(batches.values())).reshape(-1,8)[:,3:6]
    assert not np.any(np.all(normals == hidden_normal, axis=1))
    # Rendering must leave collision bounds and authored materials intact.
    assert brush['pos'] == [0,0,0] and brush['size'] == [64,64,64]
    assert brush['textures']['south'] == texture
    if not convex:
        vertices, count = build_brush_mesh({'brushes':[brush]})
        assert count == 30 and vertices.size == count*6


@pytest.mark.parametrize('convex', [False, True])
def test_all_nodraw_brush_emits_no_draw_or_shadow_geometry(convex):
    from engine.brush_geometry import box_to_geometry
    brush = {'pos':[0,0,0], 'size':[64,64,64],
             'textures':dict.fromkeys(('south','north','east','west','top','down'), 'nodraw')}
    if convex:
        box_to_geometry(brush)
    assert build_textured_batches({'brushes':[brush]}) == {}


def test_box_face_materials_match_authored_desktop_plane_normals():
    from engine.brush_geometry import box_planes
    textures = {tag:tag+'.png' for tag in ('south','north','east','west','top','down')}
    brush = {'pos':[0,0,0], 'size':[64,96,128], 'textures':textures}
    batches = build_textured_batches({'brushes':[brush]})
    for plane in box_planes(brush['pos'],brush['size'],textures):
        vertices = batches[plane['texture']].reshape(-1,8)
        np.testing.assert_allclose(vertices[:,3:6],np.tile(plane['n'],(6,1)))
        np.testing.assert_allclose(vertices[:,:3] @ plane['n'],plane['d'])


def test_mobile_light_capacity_matches_desktop_instead_of_truncating_at_32():
    from engine.shaders import MAX_LIGHTS
    world = PlayerWorld(level([{'type':'Light','pos':[i,100,0]} for i in range(MAX_LIGHTS)]))
    try:
        assert len(lights(EntityProjection().update(world),(0,100,0))) == MAX_LIGHTS
    finally:
        world.close()


def test_projection_includes_all_showcase_light_families_and_hides_disabled():
    things=[{'type':'Light','pos':[i*10,32,0], 'properties':{'state':'on'}} for i in range(15)]
    things += [{'type':'Light','pos':[0,32,0], 'properties':{'state':False}}]
    things += [{'type':'Effect','pos':[30,32,0], 'properties':{'effect_type':'FIRE'}}]
    world=PlayerWorld(level(things))
    try:
        table=EntityProjection().update(world)
        actual=lights(table,(0,0,0))
        assert len(actual)==16
        assert len(table.effect_slots)==1 and table.effect_alive[table.effect_slots[0]]
        path,elapsed,phase=effect_image(table,int(table.effect_slots[0]))
        assert path.endswith('.gif') and elapsed>=0 and 0<=phase<=1
    finally:world.close()


def test_gif_advances_and_preserves_transparency():
    first=Image.new('RGBA',(2,2),(255,0,0,255));second=Image.new('RGBA',(2,2),(0,255,0,255))
    stream=io.BytesIO();first.save(stream,format='GIF',save_all=True,append_images=[second],duration=[100,200],loop=0)
    class Package:
        def read_asset(self,name):return stream.getvalue()
    images=Images(Package())
    assert images.frame('fire.gif',0)[0]==0
    assert images.frame('fire.gif',.15)[0]==1
    assert images.frame('fire.gif',.35)[0]==0
    assert images.frame('fire.gif',.5,loop=False)[0]==1


def test_light_cutoff_is_3d_player_distance_before_cap_and_shadow_selection():
    things = [{'type':'Light','pos':position, 'properties':{'casts_shadows':True}}
              for position in ((4096,0,0),(4097,0,0),(0,4097,0),(3000,0,3000),(0,0,0))]
    things.append({'type':'Effect','pos':[0,0,-4097], 'properties':{'effect_type':'FIRE'}})
    world = PlayerWorld(level(things))
    try:
        table = EntityProjection().update(world)
        actual = lights(table,(10000,0,0),player_position=(0,0,0))
        assert [tuple(light['pos']) for light in actual] == [(4096,0,0),(0,0,0)]
        # A distant camera never changes the player-centred cutoff. Moving the
        # player brings formerly excluded lights into range next frame.
        actual = lights(table,(0,0,0),player_position=(4097,0,0),limit=1)
        assert len(actual)==1 and tuple(actual[0]['pos']) == (4096,0,0)
    finally:
        world.close()


def test_cutscene_light_range_follows_camera_and_retains_visible_influence():
    things = [{'type':'Light','pos':position, 'properties':{'radius':radius}}
              for position,radius in (((4500,0,0),1000),((5100,0,0),100),((0,0,0),100))]
    world = PlayerWorld(level(things))
    try:
        table = EntityProjection().update(world)
        # The first light's centre is beyond 4096, but its radius reaches
        # geometry in front of the engine's 3768-unit opaque fog horizon.
        actual = lights(table,(0,0,0),player_position=(10000,0,0),
                        cutscene=True,visible_horizon=3768)
        assert [tuple(light['pos']) for light in actual] == [(4500,0,0),(0,0,0)]
        # As the camera travels, the old origin light leaves its visible
        # influence range and the formerly distant light enters immediately.
        actual = lights(table,(5100,0,0),player_position=(0,0,0),
                        cutscene=True,visible_horizon=3768)
        assert [tuple(light['pos']) for light in actual] == [(4500,0,0),(5100,0,0)]
    finally:
        world.close()


def test_point_blank_hits_damage_then_kill_without_changing_weapon_balance():
    world=PlayerWorld(level([{'type':'Monster','pos':[0,2,-20], 'properties':{'health':100,'awake':False}}]))
    try:
        world.logic.player_runtime.notarget=True
        combat=world.logic.combat_runtime
        combat.give_weapon(combat.items.resolve('gun1'))
        monster=world.state.things[1]
        inp=InputState();inp.set_button(ACTION_FIRE,True)
        eye,direction,_,_=world.camera()
        world.step(1/60,inp,-90,0)
        origin,aim=combat._aim()
        np.testing.assert_allclose(aim,list(direction),atol=1e-5)
        assert monster.properties['health']==75
        for _ in range(3):world.step(1/60,inp,-90,0)
        assert monster.properties['health']==0 and monster.properties['dead']
    finally:world.close()


def test_real_pickup_activates_weapon_hud_and_muzzle_flash():
    world=PlayerWorld(level([{'type':'Prop','pos':[0,32,0], 'properties':{'collect_enabled':True,'collect_type':'item','collect_item':'gun1'}}]))
    try:
        world.step(1/60,InputState(),-90,0)
        hud=world.hud_state
        assert hud['held'].id=='gun1'
        assert hud['held'].hud_sprite.endswith('gun1HUD.png')
        class Pictures:
            def frame(self,name):
                color='yellow' if 'flash' in name else 'red'
                return 0,Image.new('RGBA',(20,40),color)
        canvas=Image.new('RGBA',(640,360))
        paint(canvas,{'game_hud':hud},Pictures())
        # Actual held sprite is visible at its authored right-hand placement.
        assert canvas.getpixel((590,300))[:3]==(255,0,0)
        hud['flash']=True
        flashed=Image.new('RGBA',(640,360))
        paint(flashed,{'game_hud':hud},Pictures())
        assert flashed.getpixel((590,300))[:3]==(255,255,0)
        hud['cinematic']=True
        cinematic=Image.new('RGBA',(640,360))
        paint(cinematic,{'game_hud':hud},Pictures())
        assert cinematic.getbbox() is None
    finally:world.close()
