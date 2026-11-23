"""Build maps/Reel_To_Reel_Test.json: the bundled reel-to-reel pack, spinning.

    python tools/make_reel_to_reel_test_map.py

A small machine room with the two mainframes of
``assets/bundled_reel_to_reel.fiopak`` against its back wall, two tape reels
on each. The wide machine's reels run from the start of Play; the button left
of the tall machine starts and stops its reels (ToggleSpin), the button right
of the wide machine reverses that machine's reels (ReverseSpin).

The reels sit on the hubs modelled on each mainframe's front (+Z) face: a
turntable disc, a collar and a spindle behind the smoked window. A reel's back
(local z -3.35) rests on the collar's front, so its spindle reaches into the
reel's hub. Both reels of a machine turn clockwise, seen from the room: the
supply reel (left, full) slower than the take-up reel (right).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from editor.editor_state import EditorState  # noqa: E402
from editor.io_system import OutputConnection, add_connection  # noqa: E402
from editor.things import Light, PlayerStart, Prop  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402
from engine.brush_placement import export_world_level  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing, stable_id  # noqa: E402

TALL = 'assets/models/reel_mainframe_tall.glb'
WIDE = 'assets/models/reel_mainframe_wide.glb'
REEL = 'assets/models/tape_reel_%s.glb'

#: Each mainframe's hub centres (model x, y) and the front of its hub collar
#: (model z), read from the GLB: the 98-vertex turntable discs, 66-vertex
#: collars and 50-vertex spindles on the front face.
HUBS = {
    TALL: {'hubs': ((-22.0, 151.0), (22.0, 151.0)), 'collar_front': 33.7},
    WIDE: {'hubs': ((-27.0, 142.0), (27.0, 142.0)), 'collar_front': 32.5},
}
#: The reel model's back face, local z (its thin axis, the spin axis).
REEL_BACK = -3.35

#: Clockwise from the room (+Z looks back at the viewer): negative about +Z.
SUPPLY_SPEED, TAKEUP_SPEED = -150.0, -210.0

W, L, H, T = 640.0, 512.0, 320.0, 32.0   # room interior, wall thickness
BACK = -L / 2                            # the back wall's face (z)


def tex(name):
    return {tag: name for tag in bg.FACE_TAGS}


def button(name, pos, colour):
    """A small coloured block on the back wall."""
    return box_brush(name, pos, (16, 16, 8), textures=tex('default.png'),
                     colour=list(colour))


def use_trigger(name, pos):
    """A use-activated trigger over a button, as maps/button_test.json has."""
    return box_brush(name, pos, (20, 20, 12), textures=tex('trigger.jpg'),
                     is_trigger=True, trigger_type='Multiple',
                     trigger_activation='use', use_label='')


wall, floor, ceiling = 'sci_fi_metal_wall_panel.jpg', 'Tile_03-512x512.png', 'concrete02.png'
brushes = [
    box_brush('floor', (0, -T / 2, 0), (W + 2 * T, T, L + 2 * T), textures=tex(floor)),
    box_brush('ceiling', (0, H + T / 2, 0), (W + 2 * T, T, L + 2 * T), textures=tex(ceiling)),
    box_brush('back_wall', (0, H / 2, BACK - T / 2), (W, H, T), textures=tex(wall)),
    box_brush('front_wall', (0, H / 2, L / 2 + T / 2), (W, H, T), textures=tex(wall)),
    box_brush('west_wall', (-W / 2 - T / 2, H / 2, 0), (T, H, L), textures=tex(wall)),
    box_brush('east_wall', (W / 2 + T / 2, H / 2, 0), (T, H, L), textures=tex(wall)),
    button('toggle_button', (-216, 64, BACK + 4), (0.8, 0.07, 0.08)),
    use_trigger('Toggle_Tall_Reels', (-216, 64, BACK + 6)),
    button('reverse_button', (216, 64, BACK + 4), (0.1, 0.28, 0.55)),
    use_trigger('Reverse_Wide_Reels', (216, 64, BACK + 6)),
]


def mainframe(name, path, x, back_z, foot_y):
    """A mainframe standing on the floor with its back against the wall.

    *back_z* is the model's rearmost z and *foot_y* its lowest y."""
    return Prop.for_model(path, pos=[x, -foot_y, BACK + 1.5 - back_z],
                          properties={'name': name, 'id': stable_id(name)})


def reels(frame, colours, names, autostart):
    """Two reels on *frame*'s hubs, facing the room, Spin on."""
    hubs = HUBS[frame.properties['model_path']]
    z = round(frame.pos[2] + hubs['collar_front'] - REEL_BACK, 3)
    out = []
    for (hx, hy), colour, name, speed in zip(
            hubs['hubs'], colours, names, (SUPPLY_SPEED, TAKEUP_SPEED)):
        out.append(Prop.for_model(
            REEL % colour, pos=[frame.pos[0] + hx, frame.pos[1] + hy, z],
            properties={
                'name': name, 'id': stable_id(name),
                # Decoration on a solid machine, out of reach: no hull to
                # keep in step with the spin.
                'no_collision': True,
                'spin_enabled': True, 'spin_axis': 'auto',
                'spin_speed': speed, 'spin_ramp': 1.0,
                'spin_autostart': autostart,
            }))
    return out


# Model bounds: tall z -30.5..36.2, y -0.5..200.5; wide z -28.5..35, y -1..193.5.
tall = mainframe('Mainframe_Tall', TALL, -120.0, -30.5, -0.5)
wide = mainframe('Mainframe_Wide', WIDE, 120.0, -28.5, -1.0)
tall_reels = reels(tall, ('blue', 'orange'), ('Tall_Reel_Supply', 'Tall_Reel_Takeup'), False)
wide_reels = reels(wide, ('cream', 'blue'), ('Wide_Reel_Supply', 'Wide_Reel_Takeup'), True)

things = [
    make_thing(PlayerStart, 'PlayerStart_1', (0.0, 56.0, 160.0), angle=90.0, primary=True),
    make_thing(Light, 'Light_Room', (0.0, 280.0, 40.0), radius=1024.0, intensity=1.3),
    make_thing(Light, 'Light_Machines', (0.0, 200.0, -120.0), radius=640.0, intensity=0.8),
    tall, wide, *tall_reels, *wide_reels,
]

state = EditorState()
state.load_from_data({'brushes': [dict(b) for b in brushes],
                      'things': [t.to_dict() for t in things]}, save_undo=False)

WIRING = {
    'Toggle_Tall_Reels': ('ToggleSpin', tall_reels),
    'Reverse_Wide_Reels': ('ReverseSpin', wide_reels),
}
for brush in state.brushes:
    if brush.get('name') in WIRING:
        input_name, targets = WIRING[brush['name']]
        for reel in targets:
            add_connection(brush, OutputConnection(
                output_name='OnTrigger', target_name=reel.properties['name'],
                input_name=input_name, target_id=reel.properties['id']))

# Shipped maps are written in the version-4 layout both builds read.
data = export_world_level(state.get_level_data())
data.pop('created', None)
with open(ROOT / 'maps' / 'Reel_To_Reel_Test.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)
    f.write('\n')
print('brushes', len(data['brushes']), 'things', [t['properties'].get('name') for t in data['things']])
