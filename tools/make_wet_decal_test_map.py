"""Build maps/Wet_Decal_Test.json: a hall to try image and wet decals in.

    python tools/make_wet_decal_test_map.py
"""
import json, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from editor.editor_state import EditorState
from editor.things import Decal, Light, PlayerStart
from engine import brush_geometry as bg
from tests.helpers.worlds import box_brush, make_thing

W, L, H, T = 1024.0, 3072.0, 288.0, 32.0
floor_tex, wall_tex = 'Tile_03-512x512.png', 'concrete02.png'
def tex(name):
    return {tag: name for tag in bg.FACE_TAGS}
brushes = [
    box_brush('floor', (0, -T / 2, 0), (W, T, L), textures=tex(floor_tex)),
    box_brush('ceiling', (0, H + T / 2, 0), (W, T, L), textures=tex(wall_tex)),
    box_brush('north_wall', (0, H / 2, L / 2 + T / 2), (W, H, T), textures=tex(wall_tex)),
    box_brush('south_wall', (0, H / 2, -L / 2 - T / 2), (W, H, T), textures=tex(wall_tex)),
    box_brush('east_wall', (W / 2 + T / 2, H / 2, 0), (T, H, L), textures=tex(wall_tex)),
    box_brush('west_wall', (-W / 2 - T / 2, H / 2, 0), (T, H, L), textures=tex(wall_tex)),
    box_brush('crate', (140, 40, 760), (80, 80, 80), textures=tex('WoodenCrate_512.jpg')),
    box_brush('pillar', (-160, 120, 640), (64, 240, 64), textures=tex('Metal_08-512x512.png')),
    box_brush('far_crate', (110, 40, -1050), (80, 80, 80), textures=tex('WoodenCrate_512.jpg')),
    # Bright things beside the near puddles, for them to reflect.
    box_brush('light_panel', (20, 150, 660), (360, 70, 16), textures=tex('StripLight.jpg')),
    box_brush('red_box', (-60, 48, 740), (64, 96, 64), textures=tex('curtains.png')),
    box_brush('far_panel', (0, 150, -1250), (360, 70, 16), textures=tex('StripLight.jpg')),
]
def decal(name, pos, image, size=192, yaw=0.0, pitch=-90.0, roll=0.0, opacity=1.0, height=None):
    return make_thing(Decal, name, pos, texture=image, width=float(size),
                      height=float(height or size), depth=48.0, yaw=yaw, pitch=pitch,
                      roll=roll, opacity=opacity)
things = [
    make_thing(PlayerStart, 'spawn', (0.0, 50.0, 1250.0), angle=90.0, primary=True),
    make_thing(Light, 'lamp_near', (0.0, 240.0, 800.0), radius=1400.0, intensity=1.6),
    make_thing(Light, 'lamp_far', (0.0, 240.0, -900.0), radius=1400.0, intensity=1.6),
    decal('Puddles_near', (20.0, 0.0, 860.0), 'wet_puddle_01.png', size=340),
    decal('Damp_floor', (-300.0, 0.0, 980.0), 'wet_damp_01.png', size=300, roll=30.0),
    decal('Grime_floor', (300.0, 0.0, 1050.0), 'grime_01.png', size=200),
    decal('Scorch_wall', (W / 2, 110.0, 980.0), 'scorch_01.png', size=170, yaw=0.0, pitch=0.0),
    decal('Damp_wall', (-W / 2, 90.0, 820.0), 'wet_damp_01.png', size=260, yaw=180.0, pitch=0.0),
    # A whole section of floor made wet: wet_full.png scaled to 1000 x 700.
    decal('Wet_floor_far', (0.0, 0.0, -850.0), 'wet_full.png', size=1000, height=700),
]
state = EditorState()
state.load_from_data({'brushes': [dict(b) for b in brushes], 'things': [t.to_dict() for t in things]})
# Shipped maps are written in the version-4 layout both builds read.
from engine.brush_placement import export_world_level
data = export_world_level(state.get_level_data())
with open(__import__('pathlib').Path(__file__).resolve().parents[1] / 'maps' / 'Wet_Decal_Test.json', 'w') as f:
    json.dump(data, f, indent=4)
print('things', [t['type'] for t in data['things']], 'version', data.get('version'))
