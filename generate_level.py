import random
import numpy as np

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
        self.map = np.full((self.height, self.width), WALL, dtype=int)
        self.rooms = []

    def _create_rooms(self, max_rooms=15, min_room_size=6, max_room_size=12):
        for _ in range(max_rooms):
            w = random.randint(min_room_size, max_room_size)
            h = random.randint(min_room_size, max_room_size)
            x = random.randint(1, self.width - w - 2)
            y = random.randint(1, self.height - h - 2)
            new_room = {"x": x, "y": y, "w": w, "h": h}
            if not any(self._rooms_intersect(new_room, other_room) for other_room in self.rooms):
                self._carve_room(new_room)
                self.rooms.append(new_room)

    def _rooms_intersect(self, room1, room2):
        return (room1["x"] < room2["x"] + room2["w"] and
                room1["x"] + room1["w"] > room2["x"] and
                room1["y"] < room2["y"] + room2["h"] and
                room1["y"] + room1["h"] > room2["y"])

    def _carve_room(self, room):
        self.map[room["y"]:room["y"] + room["h"], room["x"]:room["x"] + room["w"]] = FLOOR

    def _create_corridors(self):
        for i in range(len(self.rooms) - 1):
            room_a = self.rooms[i]
            room_b = self.rooms[i + 1]
            center_a = (room_a["x"] + room_a["w"] // 2, room_a["y"] + room_a["h"] // 2)
            center_b = (room_b["x"] + room_b["w"] // 2, room_b["y"] + room_b["h"] // 2)
            if random.random() < 0.5:
                self._carve_h_corridor(center_a[0], center_b[0], center_a[1])
                self._carve_v_corridor(center_a[1], center_b[1], center_b[0])
            else:
                self._carve_v_corridor(center_a[1], center_b[1], center_a[0])
                self._carve_h_corridor(center_a[0], center_b[0], center_b[1])

    def _carve_h_corridor(self, x1, x2, y):
        start_y = min(y, self.height - 2)
        for x in range(min(x1, x2), max(x1, x2) + 1):
            self.map[start_y:start_y+2, x] = FLOOR

    def _carve_v_corridor(self, y1, y2, x):
        start_x = min(x, self.width - 2)
        for y in range(min(y1, y2), max(y1, y2) + 1):
            self.map[y, start_x:start_x+2] = FLOOR

    def generate(self):
        self._create_rooms()
        if len(self.rooms) > 1:
            self._create_corridors()
        return self.map


class LevelGeneratorB:
    """
    Generates a map with a few small rooms and a large number of very long,
    winding corridors with repeated 90-degree bends.
    """
    def __init__(self, width=100, height=60):
        self.width = width
        self.height = height
        self.map = np.full((self.height, self.width), WALL, dtype=int)
        self.rooms = []
        # Directions: N, E, S, W -- (dy, dx) tuple format
        self.directions = [(-1, 0), (0, 1), (1, 0), (0, -1)]

    def _create_rooms(self, max_rooms, min_room_size, max_room_size):
        """Places a specified number of non-overlapping rooms on the map."""
        for _ in range(max_rooms):
            w = random.randint(min_room_size, max_room_size)
            h = random.randint(min_room_size, max_room_size)
            x = random.randint(1, self.width - w - 2)
            y = random.randint(1, self.height - h - 2)
            new_room = {"x": x, "y": y, "w": w, "h": h}
            if not any(self._rooms_intersect(new_room, other_room) for other_room in self.rooms):
                self._carve_room(new_room)
                self.rooms.append(new_room)

    def _rooms_intersect(self, room1, room2):
        return (room1["x"] < room2["x"] + room2["w"] and
                room1["x"] + room1["w"] > room2["x"] and
                room1["y"] < room2["y"] + room2["h"] and
                room1["y"] + room1["h"] > room2["y"])

    def _carve_room(self, room):
        self.map[room["y"]:room["y"] + room["h"], room["x"]:room["x"] + room["w"]] = FLOOR

    def _create_long_winding_corridor(self, start_pos, min_len, max_len, width):
        """Carves a single long corridor with many 90-degree bends."""
        y, x = start_pos
        dir_idx = random.randint(0, 3)
        corridor_len = random.randint(min_len, max_len)
        turn_countdown = random.randint(5, 15)

        for _ in range(corridor_len):
            dy, dx = self.directions[dir_idx]
            next_y, next_x = y + dy, x + dx

            # Check if the next move is safely within map boundaries
            if not (1 <= next_x < self.width - width - 1 and 1 <= next_y < self.height - width - 1):
                dir_idx = (dir_idx + random.choice([-1, 1])) % 4
                continue

            # Force a turn periodically
            turn_countdown -= 1
            if turn_countdown <= 0:
                dir_idx = (dir_idx + random.choice([-1, 1])) % 4
                turn_countdown = random.randint(5, 15)

            y, x = next_y, next_x
            self.map[y : y + width, x : x + width] = FLOOR

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
        
        return self.map

def generate(method='genA', width=100, height=60, seed=None):
    """
    Main function to generate a level using a specified method.

    Args:
        method (str): The generation method to use ('genA' or 'genB').
        width (int): The width of the map.
        height (int): The height of the map.
        seed (int, optional): A seed for the random number generator. Defaults to None.

    Returns:
        np.ndarray: The generated level map.
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    if method == 'genB':
        generator = LevelGeneratorB(width, height)
    else: # Default to genA
        generator = LevelGeneratorA(width, height)
        
    return generator.generate()