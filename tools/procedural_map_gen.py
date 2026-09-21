#!/usr/bin/env python3
"""
Command‑line procedural map generator.
Uses the generation logic from editor/procedural_generator.py
"""

import sys
import os
import json
import random
import argparse
from datetime import datetime

# Add the project root to sys.path so we can import from editor
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Import the core generation function and required constants
from editor.procedural_generator import create_map_data, CELL_SIZE, FLOOR_SURFACE, ENTITY_Y_OFFSET


def generate_map_to_file(params, output_file):
    """Generate map data and save it as JSON."""
    map_data = create_map_data(params)
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(map_data, f, indent=2)
    print(f"Map saved to {output_file}")


def run_cli(args):
    """CLI entry point: generate map from arguments and save JSON."""
    # Map size preset or direct width/height
    if args.size:
        size_map = {
            'small': (1024, 1024),
            'medium': (2048, 2048),
            'large': (4096, 4096)
        }
        world_width, world_height = size_map[args.size]
    else:
        world_width = args.width
        world_height = args.height

    params = {
        'room_count': args.room_count,
        'min_room': args.min_room,
        'max_room': args.max_room,
        'wall_tex': args.wall_tex,
        'floor_tex': args.floor_tex,
        'spawn_monsters': args.spawn_monsters,
        'monster_count': args.monster_count,
        'world_width': world_width,
        'world_height': world_height,
        'spawn_health': args.spawn_health,
        'health_count': args.health_count,
    }

    # Use provided seed or generate a random one
    if args.seed is not None:
        random.seed(args.seed)
    else:
        seed = random.randint(0, 999999)
        random.seed(seed)
        print(f"Using random seed: {seed}")

    generate_map_to_file(params, args.output)


def main():
    parser = argparse.ArgumentParser(
        description="Procedural Map Generator (command‑line)",
        epilog="Example: %(prog)s -o mymap.json --size large --room-count 20 --monster-count 8"
    )
    parser.add_argument("--output", "-o", required=True, help="Output JSON file")
    parser.add_argument("--size", choices=["small", "medium", "large"], default="medium",
                        help="Map size preset (default: medium)")
    parser.add_argument("--width", type=int, default=2048,
                        help="World width in units (ignored if --size used)")
    parser.add_argument("--height", type=int, default=2048,
                        help="World height in units (ignored if --size used)")
    parser.add_argument("--room-count", type=int, default=14,
                        help="Target number of rooms (8-24)")
    parser.add_argument("--min-room", type=int, default=256,
                        help="Min room size (world units)")
    parser.add_argument("--max-room", type=int, default=384,
                        help="Max room size (world units)")
    parser.add_argument("--wall-tex", type=str, default="default.png",
                        help="Wall texture filename")
    parser.add_argument("--floor-tex", type=str, default="default.png",
                        help="Floor texture filename")
    parser.add_argument("--spawn-monsters", action="store_true", default=True,
                        help="Spawn monsters (default: True)")
    parser.add_argument("--no-spawn-monsters", dest="spawn_monsters", action="store_false",
                        help="Disable monster spawning")
    parser.add_argument("--monster-count", type=int, default=4,
                        help="Number of monsters to spawn")
    parser.add_argument("--spawn-health", action="store_true", default=True,
                        help="Spawn health pickups (default: True)")
    parser.add_argument("--no-spawn-health", dest="spawn_health", action="store_false",
                        help="Disable health pickups")
    parser.add_argument("--health-count", type=int, default=6,
                        help="Number of health pickups to spawn")
    parser.add_argument("--seed", type=int, help="Random seed (optional)")

    args = parser.parse_args()
    run_cli(args)


if __name__ == "__main__":
    main()