import random
import numpy as np
from editor.things import PlayerStart, Light

# --- Constants for Map Elements ---
WALL = 0
FLOOR = 1

class LevelGeneratorA:
    """
    Generates a level using a room-and-corridor approach ('genA').
    This creates a balanced mix of open rooms and connecting hallways.
    """
    def __init__(self, width=100, height=60):
        self.width = width
        self.height = height
        self.brushes = []
        self.things = []
        self.rooms = []
        self.floor_locations = []

    def _create_rooms(self, max_rooms=15, min_room_size=6, max_room_size=12, wall_height=128, cell_size=128):
        for _ in range(max_rooms):
            w = random.randint(min_room_size, max_room_size)
            h = random.randint(min_room_size, max_room_size)
            x = random.randint(1, self.width - w - 2)
            y = random.randint(1, self.height - h - 2)
            new_room = {"x": x, "y": y, "w": w, "h": h}
            
            if not any(self._rooms_intersect(new_room, other_room) for other_room in self.rooms):
                self._carve_room(new_room, wall_height, cell_size)
                self.rooms.append(new_room)
                # Add floor locations for placing things
                for i in range(x, x + w):
                    for j in range(y, y + h):
                        self.floor_locations.append((i * cell_size, wall_height / 2, j * cell_size))


    def _rooms_intersect(self, room1, room2):
        return (room1["x"] < room2["x"] + room2["w"] and
                room1["x"] + room1["w"] > room2["x"] and
                room1["y"] < room2["y"] + room2["h"] and
                room1["y"] + room1["h"] > room2["y"])

    def _carve_room(self, room, wall_height, cell_size):
        pos_x = (room['x'] + room['w'] / 2) * cell_size
        pos_z = (room['y'] + room['h'] / 2) * cell_size
        size_x = room['w'] * cell_size
        size_z = room['h'] * cell_size

        self.brushes.append({
            'pos': [pos_x, wall_height / 2, pos_z],
            'size': [size_x, wall_height, size_z],
            'operation': 'add',
            'textures': {f: 'floor_tile.png' for f in ['top', 'down']}
        })


    def _create_corridors(self, wall_height=128, cell_size=128):
        for i in range(len(self.rooms) - 1):
            room_a = self.rooms[i]
            room_b = self.rooms[i + 1]
            center_a = (room_a["x"] + room_a["w"] // 2, room_a["y"] + room_a["h"] // 2)
            center_b = (room_b["x"] + room_b["w"] // 2, room_b["y"] + room_b["h"] // 2)
            
            if random.random() < 0.5:
                self._carve_h_corridor(center_a[0], center_b[0], center_a[1], wall_height, cell_size)
                self._carve_v_corridor(center_a[1], center_b[1], center_b[0], wall_height, cell_size)
            else:
                self._carve_v_corridor(center_a[1], center_b[1], center_a[0], wall_height, cell_size)
                self._carve_h_corridor(center_a[0], center_b[0], center_b[1], wall_height, cell_size)


    def _carve_h_corridor(self, x1, x2, y, wall_height, cell_size):
        pos_x = (min(x1, x2) + abs(x1 - x2) / 2) * cell_size
        pos_z = y * cell_size
        size_x = abs(x1 - x2) * cell_size
        size_z = cell_size

        self.brushes.append({
            'pos': [pos_x, wall_height / 2, pos_z],
            'size': [size_x, wall_height, size_z],
            'operation': 'add',
            'textures': {f: 'floor_tile.png' for f in ['top', 'down']}
        })
        # Add floor locations
        for i in range(min(x1,x2), max(x1,x2)+1):
            self.floor_locations.append((i * cell_size, wall_height / 2, y * cell_size))


    def _carve_v_corridor(self, y1, y2, x, wall_height, cell_size):
        pos_x = x * cell_size
        pos_z = (min(y1, y2) + abs(y1 - y2) / 2) * cell_size
        size_x = cell_size
        size_z = abs(y1 - y2) * cell_size
        self.brushes.append({
            'pos': [pos_x, wall_height / 2, pos_z],
            'size': [size_x, wall_height, size_z],
            'operation': 'add',
            'textures': {f: 'floor_tile.png' for f in ['top', 'down']}
        })
        # Add floor locations
        for i in range(min(y1,y2), max(y1,y2)+1):
            self.floor_locations.append((x * cell_size, wall_height/2, i*cell_size))

    def generate(self):
        self._create_rooms()
        if len(self.rooms) > 1:
            self._create_corridors()
        
        if self.floor_locations:
            player_pos = random.choice(self.floor_locations)
            self.things.append(PlayerStart(pos=[player_pos[0], 40, player_pos[2]]))
            for _ in range(max(1, len(self.floor_locations) // 25)):
                light_pos = random.choice(self.floor_locations)
                self.things.append(Light(pos=[light_pos[0], 128 - 40, light_pos[2]]))
        
        return self.brushes, self.things


class LevelGeneratorB:
    """
    Generates a map with a few small rooms and a large number of very long,
    winding corridors with repeated 90-degree bends.
    """
    def __init__(self, width=100, height=60):
        self.width = width
        self.height = height
        self.brushes = []
        self.things = []
        self.rooms = []
        self.floor_locations = []
        # Directions: N, E, S, W -- (dy, dx) tuple format
        self.directions = [(-1, 0), (0, 1), (1, 0), (0, -1)]

    def _create_rooms(self, max_rooms, min_room_size, max_room_size, wall_height=128, cell_size=128):
        """Places a specified number of non-overlapping rooms on the map."""
        for _ in range(max_rooms):
            w = random.randint(min_room_size, max_room_size)
            h = random.randint(min_room_size, max_room_size)
            x = random.randint(1, self.width - w - 2)
            y = random.randint(1, self.height - h - 2)
            new_room = {"x": x, "y": y, "w": w, "h": h}
            if not any(self._rooms_intersect(new_room, other_room) for other_room in self.rooms):
                self._carve_room(new_room, wall_height, cell_size)
                self.rooms.append(new_room)
                for i in range(x, x + w):
                    for j in range(y, y + h):
                        self.floor_locations.append((i * cell_size, wall_height / 2, j * cell_size))


    def _rooms_intersect(self, room1, room2):
        return (room1["x"] < room2["x"] + room2["w"] and
                room1["x"] + room1["w"] > room2["x"] and
                room1["y"] < room2["y"] + room2["h"] and
                room1["y"] + room1["h"] > room2["y"])

    def _carve_room(self, room, wall_height, cell_size):
        pos_x = (room['x'] + room['w'] / 2) * cell_size
        pos_z = (room['y'] + room['h'] / 2) * cell_size
        size_x = room['w'] * cell_size
        size_z = room['h'] * cell_size
        self.brushes.append({
            'pos': [pos_x, wall_height / 2, pos_z],
            'size': [size_x, wall_height, size_z],
            'operation': 'add',
            'textures': {f: 'floor_tile.png' for f in ['top', 'down']}
        })

    def _create_long_winding_corridor(self, start_pos, min_len, max_len, width, wall_height=128, cell_size=128):
        """Carves a single long corridor with many 90-degree bends."""
        y, x = start_pos
        dir_idx = random.randint(0, 3)
        corridor_len = random.randint(min_len, max_len)
        turn_countdown = random.randint(5, 15)
        
        current_x, current_y = x, y
        
        segment_length = 0
        
        for _ in range(corridor_len):
            dy, dx = self.directions[dir_idx]
            next_y, next_x = y + dy, x + dx

            if not (1 <= next_x < self.width - width -1 and 1 <= next_y < self.height - width -1):
                dir_idx = (dir_idx + random.choice([-1,1])) % 4
                continue

            turn_countdown -=1
            if turn_countdown <=0:
                self._create_corridor_brush(current_x, current_y, segment_length, dir_idx, width, wall_height, cell_size)
                current_x, current_y = x,y
                segment_length = 0

                dir_idx = (dir_idx + random.choice([-1, 1])) % 4
                turn_countdown = random.randint(5, 15)

            y, x = next_y, next_x
            segment_length += 1
            self.floor_locations.append((x*cell_size, wall_height/2, y*cell_size))
        
        self._create_corridor_brush(current_x, current_y, segment_length, dir_idx, width, wall_height, cell_size)


    def _create_corridor_brush(self, start_x, start_y, length, direction_idx, width, wall_height, cell_size):
        if length == 0:
            return
        
        dy, dx = self.directions[direction_idx]

        size_x = (length * abs(dx) + width * abs(dy)) * cell_size
        size_z = (length * abs(dy) + width * abs(dx)) * cell_size

        pos_x = (start_x + (length * dx)/2.0) * cell_size
        pos_z = (start_y + (length * dy)/2.0) * cell_size

        self.brushes.append({
            'pos': [pos_x, wall_height / 2, pos_z],
            'size': [size_x, wall_height, size_z],
            'operation': 'add',
            'textures': {f: 'floor_tile.png' for f in ['top', 'down']}
        })
        

    def generate(self):
        # 1. Create a small number of rooms
        self._create_rooms(max_rooms=5, min_room_size=5, max_room_size=10)

        # 2. Generate a large number of very long corridors
        num_corridors = 50
        min_len = 250
        max_len = 500
        width = 2
        
        start_points = []
        # Add a starting point from within each created room
        for room in self.rooms:
            start_points.append(
                (random.randint(room["y"], room["y"] + room["h"] - 1),
                 random.randint(room["x"], room["x"] + room["w"] - 1))
            )
        
        # Add more random starting points to reach the desired number of corridors
        for _ in range(num_corridors - len(self.rooms)):
             start_points.append(
                 (random.randint(1, self.height - width - 1),
                  random.randint(1, self.width - width - 1))
             )
        
        random.shuffle(start_points)

        # 3. Carve all the corridors
        for pos in start_points:
            self._create_long_winding_corridor(pos, min_len, max_len, width)

        if self.floor_locations:
            player_pos = random.choice(self.floor_locations)
            self.things.append(PlayerStart(pos=[player_pos[0], 40, player_pos[2]]))
            for _ in range(max(1, len(self.floor_locations) // 25)):
                light_pos = random.choice(self.floor_locations)
                self.things.append(Light(pos=[light_pos[0], 128 - 40, light_pos[2]]))
        
        return self.brushes, self.things

def generate(method='genA', width=100, height=60, seed=None):
    """
    Main function to generate a level using a specified method.

    Args:
        method (str): The generation method to use ('genA' or 'genB').
        width (int): The width of the map.
        height (int): The height of the map.
        seed (int, optional): A seed for the random number generator. Defaults to None.

    Returns:
        tuple: A tuple containing the list of brushes and the list of things.
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    if method == 'genB':
        generator = LevelGeneratorB(width, height)
    else: # Default to genA
        generator = LevelGeneratorA(width, height)
        
    return generator.generate()