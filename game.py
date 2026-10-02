import configparser
import sys
import json
import numpy as np
import math # Import math for floor/ceil
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtCore import Qt
from engine.qt_game_view import QtGameView
from editor.things import Light, PlayerStart, Thing
from engine.constants import TILE_SIZE, WALL_TILE, FLOOR_TILE

class GameWindow(QMainWindow):
    def __init__(self, level_file):
        super().__init__()
        self.setWindowTitle("Game")
        self.setGeometry(100, 100, 1280, 720)

        # Load config.ini
        self.config = configparser.ConfigParser()
        self.config.read('settings.ini')

        self.brushes = []
        self.things = []
        self.selected_object = None
        self.keys_pressed = set()

        self.game_view = QtGameView(self)
        self.setCentralWidget(self.game_view)

        # Load the level data
        self.load_level(level_file)
        
        # Find player start and launch into play mode
        player_start = None
        for thing in self.things:
            if isinstance(thing, PlayerStart):
                player_start = thing
                break
        if player_start:
            physics_enabled = self.config.getboolean('Settings', 'physics', fallback=True)
            self.game_view.toggle_play_mode(player_start.pos, player_start.get_angle(), physics_enabled)
        else:
            print("Warning: No Player Start found in the map.")


    def keyPressEvent(self, event):
        """Track pressed keys for the game view and handle mode switching."""
        self.keys_pressed.add(event.key())
        
        # Toggle game mode on 'G' press
        if event.key() == Qt.Key_G:
            self.game_view.set_game_mode(True)
        # Toggle editor mode on 'E' press
        elif event.key() == Qt.Key_E:
            self.game_view.set_game_mode(False)
        
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        """Track released keys for the game view."""
        if not event.isAutoRepeat():
            self.keys_pressed.discard(event.key())
        super().keyReleaseEvent(event)

    def load_level(self, file_path):
        try:
            with open(file_path, 'r') as f:
                level_data = json.load(f)
        except FileNotFoundError:
            print(f"Error: Level file not found at {file_path}")
            return
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON from {file_path}")
            return

        self.brushes = level_data.get('brushes', [])
        
        self.things = []
        player_start_pos = [0, TILE_SIZE, 0] # Default player start height
        player_start_angle = -90.0
        for thing_data in level_data.get('things', []):
            thing = Thing.from_dict(thing_data)
            if thing:
                self.things.append(thing)
                if isinstance(thing, PlayerStart):
                    player_start_pos = thing.pos
                    player_start_angle = thing.get_angle()


        # Initialize editor camera at player start location
        self.game_view.camera.pos = [player_start_pos[0], player_start_pos[1] + 20, player_start_pos[2]]
        self.game_view.camera.yaw = player_start_angle
        print("Level loaded. Editor camera positioned at Player Start.")

        # --- Tilemap generation is skipped ---
        self.game_view.set_tile_map(None)
        print("Skipping tilemap generation for direct play.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python game.py <path_to_level.json>")
        sys.exit(1)

    app = QApplication(sys.argv)
    level_file = sys.argv[1]
    window = GameWindow(level_file)
    window.show()
    sys.exit(app.exec_())