import json
import os
import math
import numpy as np
import sys
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QGraphicsView, QGraphicsScene,
                             QFileDialog)
from PyQt5.QtCore import Qt, QRectF, QTimer, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QColor, QBrush, QPen, QPainter, QFont, QImage, QPixmap

# Constants (ensure these match /engine/constants.py)
TILE_SIZE = 50.0
WALL_TILE = 0
FLOOR_TILE = 1

class TilemapGenerator:
    def __init__(self):
        self.brushes = []
        self.tile_map = None
        # Store the world coordinates of the overall map's extent
        self.min_x_world = float('inf')
        self.max_x_world = float('-inf')
        self.min_z_world = float('inf')
        self.max_z_world = float('-inf')

        # Store the padded world coordinates that define the tilemap grid
        self.padded_min_x = 0
        self.padded_max_x = 0
        self.padded_min_z = 0
        self.padded_max_z = 0

        # Store the tile indices for the map's extent
        self.min_x_tile_idx = 0
        self.min_z_tile_idx = 0

        self.current_step = 0 # Step counter for visualization
        self.map_width_tiles = 0
        self.map_depth_tiles = 0

    def load_level_data(self, json_file_path):
        """Loads brush data from a JSON level file."""
        try:
            with open(json_file_path, 'r') as f:
                level_data = json.load(f)
            self.brushes = level_data.get('brushes', [])
            self.reset_generation_steps()
            print(f"Loaded {len(self.brushes)} brushes from {json_file_path}")
            return True
        except FileNotFoundError:
            print(f"Error: Level file not found at {json_file_path}")
            return False
        except json.JSONDecodeError:
            print(f"Error: Could not decode JSON from {json_file_path}")
            return False
        except Exception as e:
            print(f"An unexpected error occurred loading level data: {e}")
            return False

    def reset_generation_steps(self):
        """Resets the state for a new generation visualization."""
        self.tile_map = None
        self.min_x_world = float('inf')
        self.max_x_world = float('-inf')
        self.min_z_world = float('inf')
        self.max_z_world = float('-inf')
        self.padded_min_x = 0
        self.padded_max_x = 0
        self.padded_min_z = 0
        self.padded_max_z = 0
        self.min_x_tile_idx = 0
        self.min_z_tile_idx = 0
        self.current_step = 0 # Start at step 0 (initial state)
        self.map_width_tiles = 0
        self.map_depth_tiles = 0


    def generate_tilemap_step(self):
        """
        Performs one step of the tilemap generation process for visualization.
        Returns a description of the current step and the data for visualization.
        """
        if not self.brushes:
            return "No brushes loaded. Please load a JSON file first.", None

        step_description = ""
        visual_data = {
            'tile_map': None, # This will be updated as steps progress
            'brushes': self.brushes,
            'current_brush_index': -1,
            'world_map_min_x': self.padded_min_x, # Used as the offset for drawing brushes
            'world_map_min_z': self.padded_min_z, # Used as the offset for drawing brushes
            'current_step': self.current_step # Add current step for visualization logic
        }

        num_brush_steps = len(self.brushes) # Total steps for brushes

        # Step 0: Initial state (after loading)
        if self.current_step == 0:
            step_description = (
                "<b>Step 1: Determine Global World Extents (X and Z)</b><br>"
                "The tool iterates through all 3D brushes to find the absolute minimum and maximum "
                "X and Z coordinates. These define the smallest rectangular area that encompasses "
                "all your level's geometry."
            )
            # Calculate extents here, but visual_data will reflect it after processing
            for brush in self.brushes:
                pos, size = np.array(brush['pos']), np.array(brush['size'])
                half_size = size / 2.0
                
                self.min_x_world = min(self.min_x_world, pos[0] - half_size[0])
                self.max_x_world = max(self.max_x_world, pos[0] + half_size[0])
                self.min_z_world = min(self.min_z_world, pos[2] - half_size[2])
                self.max_z_world = max(self.max_z_world, pos[2] + half_size[2])
            
            visual_data.update({
                'min_x_world': self.min_x_world, 'max_x_world': self.max_x_world,
                'min_z_world': self.min_z_world, 'max_z_world': self.max_z_world
            })


        # Step 1: Apply Padding and Calculate Tilemap Dimensions
        elif self.current_step == 1:
            step_description = (
                "<b>Step 2: Apply Padding and Calculate Tilemap Grid Dimensions</b><br>"
                "A padding (typically 2 tiles) is added to the global world extents. This ensures "
                "the generated 2D tilemap covers a slightly larger area than the brushes, providing "
                "buffer space for player movement near edges. These padded bounds are then converted "
                "into integer tile indices and dimensions for the grid array."
            )
            padding = TILE_SIZE * 2 # Consistent with game.py logic
            self.padded_min_x = self.min_x_world - padding
            self.padded_max_x = self.max_x_world + padding
            self.padded_min_z = self.min_z_world - padding
            self.padded_max_z = self.max_z_world + padding

            # Calculate tile indices for the padded world bounds
            # Floor division ensures tiles start at precise grid lines
            self.min_x_tile_idx = int(math.floor(self.padded_min_x / TILE_SIZE))
            self.max_x_tile_idx = int(math.ceil(self.padded_max_x / TILE_SIZE))
            self.min_z_tile_idx = int(math.floor(self.padded_min_z / TILE_SIZE))
            self.max_z_tile_idx = int(math.ceil(self.padded_max_z / TILE_SIZE))

            # Dimensions of the tilemap array
            map_width_tiles = self.max_x_tile_idx - self.min_x_tile_idx
            map_depth_tiles = self.max_z_tile_idx - self.min_z_tile_idx

            # Ensure minimum map size to avoid issues with very small or empty levels
            self.map_width_tiles = max(1, map_width_tiles)
            self.map_depth_tiles = max(1, map_depth_tiles)
            
            visual_data.update({
                'min_x_world': self.min_x_world, 'max_x_world': self.max_x_world, # Original for brush drawing
                'min_z_world': self.min_z_world, 'max_z_world': self.max_z_world,
                'padded_min_x': self.padded_min_x, 'padded_max_x': self.padded_max_x, # For drawing padded bounds
                'padded_min_z': self.padded_min_z, 'padded_max_z': self.padded_max_z,
                'map_width_tiles': self.map_width_tiles,
                'map_depth_tiles': self.map_depth_tiles,
                'tile_map_origin_x_world': self.min_x_tile_idx * TILE_SIZE, # World coord of tilemap (0,0)
                'tile_map_origin_z_world': self.min_z_tile_idx * TILE_SIZE
            })


        # Step 2: Initialize Tilemap with Floor Tiles
        elif self.current_step == 2:
            step_description = (
                "<b>Step 3: Initialize 2D Tilemap with Floor Tiles</b><br>"
                "A new 2D NumPy array, the 'collision_tile_map', is created with the calculated "
                "dimensions. Every cell in this array is initially filled with 'FLOOR_TILE' (1), "
                "designating all areas as walkable by default. This forms the base grid."
            )
            # Create the tile_map array with dimensions based on calculated tile indices
            self.tile_map = np.full((self.map_depth_tiles, self.map_width_tiles), FLOOR_TILE, dtype=int)
            visual_data.update({
                'tile_map': self.tile_map,
                'tile_map_origin_x_world': self.min_x_tile_idx * TILE_SIZE,
                'tile_map_origin_z_world': self.min_z_tile_idx * TILE_SIZE,
                'map_width_tiles': self.map_width_tiles, # Needed for drawing grid
                'map_depth_tiles': self.map_depth_tiles
            })
        
        # Step 3 onwards: Process brushes
        # The first brush is at self.current_step == 3
        # The last brush is at self.current_step == 3 + num_brush_steps - 1
        elif self.current_step >= 3 and self.current_step < (3 + num_brush_steps):
            brush_index = self.current_step - 3
            brush = self.brushes[brush_index]
            
            step_description = (
                f"<b>Step 4: Processing Brush {brush_index + 1}/{num_brush_steps}</b><br>"
                "Each brush's horizontal projection is determined. If the brush is solid (not a "
                "trigger or subtractive operation), the 2D tiles it covers are marked as "
                "'WALL_TILE' (0) in the collision map. This marks impassable areas. "
            )
            visual_data['current_brush_index'] = brush_index
            visual_data.update({
                'tile_map': self.tile_map, # Pass current state of tile_map
                'tile_map_origin_x_world': self.min_x_tile_idx * TILE_SIZE,
                'tile_map_origin_z_world': self.min_z_tile_idx * TILE_SIZE,
                'map_width_tiles': self.map_width_tiles, # Needed for drawing grid
                'map_depth_tiles': self.map_depth_tiles
            })

            # Process brush if it's not a trigger or subtractive
            if brush.get('is_trigger', False) or brush.get('operation') == 'subtract':
                step_description += " (SKIPPED: Trigger or Subtractive Brush)"
                visual_data['is_brush_skipped'] = True # For visualization
            else:
                visual_data['is_brush_skipped'] = False
                pos, size = np.array(brush['pos']), np.array(brush['size'])
                half_size = size / 2.0
                
                # Convert brush world coordinates to tile indices relative to the map's tile index origin
                brush_min_x_world = pos[0] - half_size[0]
                brush_max_x_world = pos[0] + half_size[0]
                brush_min_z_world = pos[2] - half_size[2]
                brush_max_z_world = pos[2] + half_size[2]

                brush_min_x_tile = int(math.floor(brush_min_x_world / TILE_SIZE) - self.min_x_tile_idx)
                brush_max_x_tile = int(math.ceil(brush_max_x_world / TILE_SIZE) - self.min_x_tile_idx)
                brush_min_z_tile = int(math.floor(brush_min_z_world / TILE_SIZE) - self.min_z_tile_idx)
                brush_max_z_tile = int(math.ceil(brush_max_z_world / TILE_SIZE) - self.min_z_tile_idx)
                
                # Clamp to map dimensions to prevent out-of-bounds array access
                min_x_idx_clamped = max(0, brush_min_x_tile)
                max_x_idx_clamped = min(self.map_width_tiles, brush_max_x_tile) # max is exclusive for slicing
                min_z_idx_clamped = max(0, brush_min_z_tile)
                max_z_idx_clamped = min(self.map_depth_tiles, brush_max_z_tile) # max is exclusive for slicing

                # Mark tiles as walls in the tile_map
                if self.tile_map is not None:
                    # Use slicing for efficiency
                    if min_x_idx_clamped < max_x_idx_clamped and min_z_idx_clamped < max_z_idx_clamped:
                        self.tile_map[min_z_idx_clamped:max_z_idx_clamped, min_x_idx_clamped:max_x_idx_clamped] = WALL_TILE
                        # Store the exact tile indices marked by this brush for visualization highlighting
                        visual_data['marked_tiles_x'] = (min_x_idx_clamped, max_x_idx_clamped)
                        visual_data['marked_tiles_z'] = (min_z_idx_clamped, max_z_idx_clamped)
                    else:
                        visual_data['marked_tiles_x'] = None # No tiles marked
                        visual_data['marked_tiles_z'] = None


        # Final Step: Process Complete
        else:
            step_description = (
                "<b>Process Complete: Final 2D Collision Tilemap Generated</b><br>"
                "The 2D collision tilemap is now complete, representing all walkable (floor) "
                "and non-walkable (wall) areas of your 3D level. This optimized grid is used "
                "by the game for efficient player collision detection."
            )
            # This is the last step. Set current_step to -1 and do NOT increment it further.
            self.current_step = -1 
            visual_data.update({
                'tile_map': self.tile_map, # Final tile_map
                'tile_map_origin_x_world': self.min_x_tile_idx * TILE_SIZE,
                'tile_map_origin_z_world': self.min_z_tile_idx * TILE_SIZE,
                'map_width_tiles': self.map_width_tiles, # Needed for drawing grid
                'map_depth_tiles': self.map_depth_tiles
            })
            # DO NOT increment self.current_step += 1 here for the final state

        # Increment current_step ONLY if not in the final state (i.e., not -1)
        if self.current_step != -1:
            self.current_step += 1
            
        return step_description, visual_data

# --- PyQt5 GUI Structure ---

class VisualizationWidget(QGraphicsView):
    """
    A QGraphicsView subclass to visualize the tilemap generation process.
    This widget draws the 2D tilemap and the 2D projections of the 3D brushes.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.setRenderHint(QPainter.Antialiasing)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
        
        self.visual_data = None
        self.temp_items = [] # To store temporary items for animation
        self.processed_brushes_indices = set() # Store indices of processed brushes

    def update_visualization(self, visual_data):
        """Updates the widget with new visualization data for a step."""
        self.visual_data = visual_data
        self.scene_obj.clear()
        self.temp_items.clear()

        if not self.visual_data:
            return

        current_step = self.visual_data.get('current_step', 0)

        # If a brush was just processed, add its index to the set
        # Brushes are processed from step 3 onwards
        if current_step >= 4: # current_step 3 is the first brush processing, so 4 is after it's processed
            # The brush processed in the *previous* step (current_step - 1) corresponds to index (current_step - 1) - 3
            prev_brush_index = (current_step - 1) - 3
            if prev_brush_index >= 0 and not self.visual_data.get('is_brush_skipped', False):
                self.processed_brushes_indices.add(prev_brush_index)


        # Calculate world offsets for brushes to align with tilemap (0,0)
        # The tilemap's (0,0) in QGraphicsScene corresponds to this world coordinate
        world_offset_x = self.visual_data.get('tile_map_origin_x_world', 0)
        world_offset_z = self.visual_data.get('tile_map_origin_z_world', 0)

        # 1. Draw the 2D tilemap if available
        if self.visual_data.get('tile_map') is not None:
            tile_map = self.visual_data['tile_map']
            map_depth, map_width = tile_map.shape # (depth, width)

            # Set scene rect based on the actual size of the tilemap
            self.scene_obj.setSceneRect(0, 0, map_width * TILE_SIZE, map_depth * TILE_SIZE)

            for z_idx in range(map_depth):
                for x_idx in range(map_width):
                    tile_type = tile_map[z_idx, x_idx]
                    
                    fill_color = QColor(180, 180, 180) # Default for floor
                    if tile_type == WALL_TILE:
                        fill_color = QColor(90, 90, 90) # Darker for wall

                    # For initial tilemap creation (Step 2), make it more noticeable
                    if current_step == 2: # This is when the tilemap is first fully initialized
                        # Draw tiles with a slightly different appearance when first initialized
                        temp_fill_color = QColor(150, 200, 255, 100) # Light blue with transparency
                        self.scene_obj.addRect(
                            x_idx * TILE_SIZE, z_idx * TILE_SIZE, TILE_SIZE, TILE_SIZE,
                            QPen(QColor(120, 120, 120), 0.5), # Thin border for grid
                            QBrush(temp_fill_color)
                        )
                        
                    # Add tile rectangle with final color
                    self.scene_obj.addRect(
                        x_idx * TILE_SIZE,  # X position in scene
                        z_idx * TILE_SIZE,  # Y position (Z in 3D) in scene
                        TILE_SIZE, TILE_SIZE,
                        QPen(QColor(120, 120, 120), 0.5), # Thin border for grid
                        QBrush(fill_color)
                    )
            
            # Highlight tiles marked by the current brush
            if self.visual_data.get('marked_tiles_x') is not None and self.visual_data.get('marked_tiles_z') is not None:
                min_x, max_x = self.visual_data['marked_tiles_x']
                min_z, max_z = self.visual_data['marked_tiles_z']
                
                highlight_rect = QRectF(
                    min_x * TILE_SIZE, min_z * TILE_SIZE,
                    (max_x - min_x) * TILE_SIZE, (max_z - min_z) * TILE_SIZE
                )
                # Make highlight more prominent for the current step
                highlight_pen = QPen(QColor(255, 255, 0), 6) # Thicker yellow highlight
                self.scene_obj.addRect(highlight_rect, highlight_pen)


        # 2. Draw global world extents and padded bounds
        if self.visual_data.get('min_x_world') is not None and self.visual_data.get('max_x_world') is not None:
            # Original world bounds (before padding)
            original_bounds_rect = QRectF(
                self.visual_data['min_x_world'] - world_offset_x,
                self.visual_data['min_z_world'] - world_offset_z,
                self.visual_data['max_x_world'] - self.visual_data['min_x_world'],
                self.visual_data['max_z_world'] - self.visual_data['min_z_world']
            )
            # Make initial bounds more visually distinct when first shown (Step 0)
            if current_step == 0:
                self.scene_obj.addRect(original_bounds_rect, QPen(QColor(0, 255, 0), 4, Qt.SolidLine)) # Thicker solid green
            else:
                self.scene_obj.addRect(original_bounds_rect, QPen(QColor(0, 255, 0), 2, Qt.DotLine)) # Green dotted line

            # Padded bounds (after padding)
            if self.visual_data.get('padded_min_x') is not None:
                padded_bounds_rect = QRectF(
                    self.visual_data['padded_min_x'] - world_offset_x,
                    self.visual_data['padded_min_z'] - world_offset_z,
                    self.visual_data['padded_max_x'] - self.visual_data['padded_min_x'],
                    self.visual_data['padded_max_z'] - self.visual_data['padded_min_z']
                )
                # Make padded bounds more visually distinct when first shown (Step 1)
                if current_step == 1:
                    self.scene_obj.addRect(padded_bounds_rect, QPen(QColor(0, 200, 255), 4, Qt.SolidLine)) # Thicker solid cyan
                else:
                    self.scene_obj.addRect(padded_bounds_rect, QPen(QColor(0, 200, 255), 2, Qt.DashLine)) # Cyan dashed line

        # 3. Draw all brushes (their 2D projection)
        if self.visual_data.get('brushes') and world_offset_x is not None:
            current_brush_index = self.visual_data.get('current_brush_index', -1)
            for i, brush in enumerate(self.visual_data['brushes']):
                pos, size = np.array(brush['pos']), np.array(brush['size'])
                half_size = size / 2.0
                
                # Calculate brush position relative to the tilemap's scene origin
                brush_rect = QRectF(
                    (pos[0] - half_size[0]) - world_offset_x,
                    (pos[2] - half_size[2]) - world_offset_z,
                    size[0],
                    size[2]
                )
                
                brush_color = QColor(0, 0, 255, 80) # Semi-transparent blue for brushes
                brush_pen = QPen(QColor(0, 0, 150), 1)

                # Check if this brush has already been processed
                if i in self.processed_brushes_indices:
                    brush_color = QColor(255, 255, 0, 150) # Processed: Opaque yellow
                    brush_pen = QPen(QColor(200, 200, 0), 2) # Yellow border

                # Highlight current brush being processed
                if i == current_brush_index:
                    brush_pen = QPen(QColor(255, 255, 0), 4) # Thicker yellow outline
                    brush_color = QColor(255, 255, 0, 150) # More opaque yellow
                    
                    # Indicate skipped brushes (triggers or subtractive)
                    if self.visual_data.get('is_brush_skipped', False):
                        brush_pen = QPen(QColor(255, 0, 0), 4, Qt.DashDotLine) # Thicker red dashed for skipped
                        brush_color = QColor(255, 0, 0, 120)

                self.scene_obj.addRect(brush_rect, brush_pen, QBrush(brush_color))

        # 4. Draw legend if process is complete
        # Only draw legend if NOT in auto-run mode, as UI will be hidden
        if current_step == -1 and not self.parent().auto_run_mode: # Access parent window's auto_run_mode
            self.draw_legend()

        # Fit all content in view
        self.fitInView(self.scene_obj.sceneRect(), Qt.KeepAspectRatio)

    def draw_legend(self):
        # Determine a good position for the legend
        scene_rect = self.scene_obj.sceneRect()
        legend_x = scene_rect.x() + 10 # 10 pixels from the left edge of the scene
        legend_y = scene_rect.y() + 10 # 10 pixels from the top edge of the scene
        
        # Add a background rectangle for the legend
        legend_bg_width = 200
        legend_bg_height = 80
        self.scene_obj.addRect(legend_x, legend_y, legend_bg_width, legend_bg_height,
                               QPen(Qt.NoPen), QBrush(QColor(255, 255, 255, 200))) # Semi-transparent white background

        font = QFont()
        font.setPointSize(12)

        # Floor Tile
        floor_color = QColor(180, 180, 180)
        self.scene_obj.addRect(legend_x + 10, legend_y + 10, 20, 20,
                               QPen(QColor(120, 120, 120), 0.5), QBrush(floor_color))
        floor_text = self.scene_obj.addText("Floor Tile", font)
        floor_text.setPos(legend_x + 40, legend_y + 10)

        # Wall Tile
        wall_color = QColor(90, 90, 90)
        self.scene_obj.addRect(legend_x + 10, legend_y + 40, 20, 20,
                               QPen(QColor(120, 120, 120), 0.5), QBrush(wall_color))
        wall_text = self.scene_obj.addText("Wall Tile", font)
        wall_text.setPos(legend_x + 40, legend_y + 40)


class TilemapVisualizationWindow(QMainWindow):
    def __init__(self, map_file_path=None): # Added map_file_path argument
        super().__init__()
        self.setWindowTitle("2D Tilemap Generation Visualizer")
        self.setGeometry(100, 100, 1000, 1000) # Increased size for better view

        self.tilemap_generator = TilemapGenerator()
        self.auto_run_mode = False # New flag for the special mode
        self.input_map_file_path = map_file_path # Store the input file path

        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self.show_next_step)
        self.animation_interval = 200 

        self.init_ui()

        # Handle initial loading based on provided path or default
        if map_file_path:
            self.auto_run_mode = True  # Enable auto-run mode
            print(f"Auto-run mode activated with file: {map_file_path}") # Debug print
            self.load_and_reset_visualization(map_file_path) # This calls show_next_step() for step 0

            if self.tilemap_generator.brushes: # Only start if brushes were successfully loaded
                # Skip to step 3 for auto-run mode
                self.tilemap_generator.current_step = 0 # This ensures we start from the beginning of generation sequence
                self.show_next_step() # Call 1 (processes step 0, sets current_step to 1)
                self.show_next_step() # Call 2 (processes step 1, sets current_step to 2)
                self.show_next_step() # Call 3 (processes step 2, sets current_step to 3)
                self.start_animation() # Starts timer which will call show_next_step repeatedly
        else:
            # Default file to load for convenience
            script_dir = os.path.dirname(__file__)
            self.default_json_file = os.path.join(script_dir, "RStudio-dev/maps/Simple_Map_Test.json") 
            if not os.path.exists(self.default_json_file):
                 # Fallback if the RStudio-dev structure isn't present
                 print(f"Warning: Default map path '{self.default_json_file}' not found. Please browse manually or ensure 'RStudio-dev/maps/' exists relative to script.")
                 self.default_json_file = "" # Clear default if not found
            
            if self.default_json_file:
                self.load_and_reset_visualization(self.default_json_file) # Load default on startup
            else:
                self.step_label.setText("No default JSON file found. Please browse to load a level file.")


    def init_ui(self):
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        self.step_label = QLabel("Click 'Browse' to select a JSON level file or use the default.")
        self.step_label.setWordWrap(True) # Allow text wrapping for long descriptions
        # Set font size for the step_label
        self.step_label.setStyleSheet("font-size: 18px;") # Increased font size
        self.layout.addWidget(self.step_label)

        self.visualization_widget = VisualizationWidget(self) # Pass self as parent
        self.layout.addWidget(self.visualization_widget)

        # Create a horizontal layout for the buttons
        self.buttons_layout = QHBoxLayout()

        self.browse_button = QPushButton("Browse JSON Level File")
        self.browse_button.clicked.connect(self.browse_json_file)
        self.buttons_layout.addWidget(self.browse_button) # Add to horizontal layout

        self.next_step_button = QPushButton("Next Step")
        self.next_step_button.clicked.connect(self.show_next_step_manual) # Connect to manual step
        self.next_step_button.setEnabled(False) # Disabled until file loaded
        
        # Set stylesheet for blue background and white text
        self.next_step_button.setStyleSheet(
            "QPushButton {"
            "   background-color: #2196F3;"  # Blue color
            "   color: white;"
            "   border-radius: 5px;"
            "   padding: 10px;"
            "}"
            "QPushButton:hover {"
            "   background-color: #0B7CD8;" # Slightly darker blue on hover
            "}"
            "QPushButton:disabled {"
            "   background-color: #BBDEFB;" # Lighter blue when disabled
            "   color: #607D8B;"
            "}"
        )
        self.buttons_layout.addWidget(self.next_step_button) # Add to horizontal layout

        # The pause button will now function as a "Pause Animation" button, but starts hidden
        self.pause_button = QPushButton("Pause Animation")
        self.pause_button.clicked.connect(self.toggle_animation)
        self.pause_button.setVisible(False) # Start hidden
        self.pause_button.setEnabled(False) # Disabled initially
        self.buttons_layout.addWidget(self.pause_button)

        self.layout.addLayout(self.buttons_layout) # Add the horizontal layout to the main vertical layout

        # Hide UI elements if in auto-run mode at initialization
        self.update_ui_visibility()

    def update_ui_visibility(self):
        """Hides or shows UI elements based on auto_run_mode."""
        if self.auto_run_mode:
            self.step_label.setVisible(False)
            self.browse_button.setVisible(False)
            self.next_step_button.setVisible(False)
            self.pause_button.setVisible(False)
            self.setGeometry(100, 100, 600, 600)
        else:
            self.step_label.setVisible(True)
            self.browse_button.setVisible(True)
            self.next_step_button.setVisible(True) # State set by other functions
            self.pause_button.setVisible(True) # State set by other functions
            self.showNormal() # Restore normal window size if not auto-run


    def load_and_reset_visualization(self, file_path):
        """Loads the JSON file and resets the visualization."""
        print(f"Attempting to load and reset visualization for: {file_path}") # Debug print
        if self.tilemap_generator.load_level_data(file_path):
            # Update UI visibility immediately after setting auto_run_mode
            self.update_ui_visibility() 
            
            if not self.auto_run_mode:
                self.step_label.setText(f"JSON '{file_path}' loaded. Click 'Next Step' to begin or wait for auto-play.")
                self.next_step_button.setEnabled(True)
                self.next_step_button.setVisible(True) # Ensure it's visible at the start
                self.pause_button.setVisible(False) # Ensure hidden at start
                self.pause_button.setEnabled(False) 
            self.animation_timer.stop() # Stop any running animation
            # Reset step counter in generator and immediately show step 0
            self.tilemap_generator.current_step = 0 
            self.visualization_widget.processed_brushes_indices.clear() # Clear processed brushes on new load
            self.show_next_step() 
            return True # Indicate successful load
        else:
            print(f"Failed to load {file_path}. Auto-run mode will not proceed.") # Debug print
            if not self.auto_run_mode:
                self.step_label.setText(f"Failed to load '{file_path}'. Please check the path.")
                self.next_step_button.setEnabled(False)
                self.next_step_button.setVisible(True)
                self.pause_button.setEnabled(False)
                self.pause_button.setVisible(False)
            self.animation_timer.stop()
            return False # Indicate failed load

    def start_animation(self):
        """Starts the animation for the visualization."""
        if self.tilemap_generator.current_step != -1:
            print(f"Starting animation with interval: {self.animation_interval}ms") # Debug print
            self.animation_timer.start(self.animation_interval)
            if not self.auto_run_mode:
                self.pause_button.setText("Pause Animation")
                self.next_step_button.setEnabled(False)
                self.next_step_button.setVisible(False)
                self.pause_button.setVisible(True)
                self.pause_button.setEnabled(True)
            # No need to call show_next_step() here, the timer will trigger it after the first interval
            # unless we want the very first animation step to be immediate, which we already handle for steps 0-2
        else:
            print("Animation not started: Process already complete.") # Debug print

    def browse_json_file(self):
        """Opens a file dialog to select a JSON level file."""
        # Start the file dialog in the 'maps' directory if it exists
        script_dir = os.path.dirname(__file__)
        initial_dir = os.path.join(script_dir, "RStudio-dev/maps")
        if not os.path.exists(initial_dir):
            initial_dir = script_dir # Fallback to script directory

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open JSON Level File", initial_dir, "JSON Files (*.json)"
        )
        if file_path:
            self.input_map_file_path = file_path # Store the new file path
            # Crucially, set auto_run_mode to False when Browse manually
            self.auto_run_mode = False 
            print(f"Selected file: {self.input_map_file_path}. Manual mode enabled.") # Debug print
            if self.load_and_reset_visualization(file_path):
                # Update UI visibility after setting auto_run_mode and loading
                self.update_ui_visibility()


    def show_next_step_manual(self):
        """Advances the visualization to the next step, triggered by manual button click."""
        print("Manual 'Next Step' clicked.") # Debug print
        self.animation_timer.stop() # Stop animation if manual step is clicked
        if not self.auto_run_mode:
            self.pause_button.setText("Resume Animation") # Reset button text
            self.pause_button.setVisible(True) # Ensure pause button is visible if manual stepping is used
            self.pause_button.setEnabled(True)
        self.show_next_step() 


    def show_next_step(self):
        """Advances the visualization to the next step."""
        current_gen_step_before = self.tilemap_generator.current_step # Debug
        step_description, visual_data = self.tilemap_generator.generate_tilemap_step()
        current_gen_step_after = self.tilemap_generator.current_step # Debug

        print(f"Processing step: {current_gen_step_before} -> {current_gen_step_after} (GUI current_step={visual_data.get('current_step')})") # Debug print

        if not self.auto_run_mode:
            self.step_label.setText(step_description)
        self.visualization_widget.update_visualization(visual_data)
        
        # Check if the process is complete (current_step is now -1)
        if self.tilemap_generator.current_step == -1: 
            print("Process finished. Checking save conditions...") # Debug print
            if not self.auto_run_mode:
                self.next_step_button.setEnabled(False)
                self.next_step_button.setVisible(False) # Hide the next step button at the final step
                self.pause_button.setEnabled(False)
                self.pause_button.setVisible(False) # Hide pause button at the end
                self.browse_button.setEnabled(True) # Re-enable browse button
                self.step_label.setText("Completed! Tilemap has been generated. Click 'Browse' to load a new map.") # Final message
            self.animation_timer.stop()

            # Save tilemap and image if in auto-run mode
            if self.auto_run_mode and self.tilemap_generator.tile_map is not None:
                print(f"Process complete in auto-run mode for {self.input_map_file_path}. Attempting to save files...") # Debug print
                # Verify tilemap content before saving
                print(f"Tilemap before saving (first 5x5 block):\n{self.tilemap_generator.tile_map[:5, :5]}") # Debug print
                self.save_tilemap_data()
                self.save_tilemap_image()
                # Exit the application after saving in auto-run mode
                print("Auto-run mode complete. Exiting application.") # Debug print
                QApplication.instance().quit() # or sys.exit(app.exec_()) if using it directly, but this is safer with QApplication instance
            elif self.auto_run_mode and self.tilemap_generator.tile_map is None:
                print("Auto-run mode finished, but tile_map is None. Files not saved.") # Debug print
                QApplication.instance().quit() # Still quit if it fails to save
            elif not self.auto_run_mode:
                print("Process finished in manual mode. Files not saved automatically.") # Debug print


        # If we are entering or are in the brush processing phase (Step 3 onwards) and animation is not explicitly paused
        # This condition will NOT be met if current_step is -1
        elif self.tilemap_generator.current_step >= 3: 
            if not self.animation_timer.isActive():
                print("Animation timer was not active, starting now for brush processing.") # Debug print
                self.animation_timer.start(self.animation_interval)
                if not self.auto_run_mode:
                    self.pause_button.setText("Pause Animation")
            
            if not self.auto_run_mode:
                self.next_step_button.setEnabled(False) # Disable manual next step during auto-play
                self.next_step_button.setVisible(False) # Hide next step button
                self.pause_button.setVisible(True) # Show pause button
                self.pause_button.setEnabled(True) # Enable pause button
        else: # Steps 0, 1, 2 (initial setup, not auto-run)
            print("Initial setup step (0, 1, or 2). Timer stopped for manual progression or initial setup.") # Debug print
            # In auto-run mode, these initial steps are executed rapidly without timer pause.
            # So this 'else' block's timer.stop() logic only applies to manual mode.
            if not self.auto_run_mode:
                self.next_step_button.setEnabled(True) # Manual stepping enabled for initial steps
                self.next_step_button.setVisible(True)
                self.pause_button.setEnabled(False) # No animation yet
                self.pause_button.setVisible(False)
            # This timer.stop() here is fine as it applies to initial manual steps,
            # but in auto-run mode, we ensure the timer is started after these.
            self.animation_timer.stop()


    def toggle_animation(self):
        """Toggles the animation on/off."""
        if self.animation_timer.isActive():
            print("Pausing animation.") # Debug print
            self.animation_timer.stop()
            if not self.auto_run_mode:
                self.pause_button.setText("Resume Animation")
                # When paused, re-enable manual next step if not at the very end
                if self.tilemap_generator.current_step != -1:
                    self.next_step_button.setEnabled(True)
                    self.next_step_button.setVisible(True)
        else:
            # Check if there are still steps to process (not final step)
            if self.tilemap_generator.current_step != -1:
                print("Resuming animation.") # Debug print
                self.animation_timer.start(self.animation_interval)
                if not self.auto_run_mode:
                    self.pause_button.setText("Pause Animation")
                    self.next_step_button.setEnabled(False) # Disable manual stepping during animation
                    self.next_step_button.setVisible(False)
                    self.show_next_step() # Immediately show the next step when starting animation
            else:
                print("Cannot resume animation: Process already complete.") # Debug print
                if not self.auto_run_mode:
                    self.step_label.setText("All brushes have been processed.") # Changed message
                    self.next_step_button.setEnabled(False) # Ensure it's disabled
                    self.next_step_button.setVisible(False) # Ensure it's hidden
                    self.pause_button.setEnabled(False)
                    self.pause_button.setVisible(False)

    def save_tilemap_data(self):
        """Saves the tilemap NumPy array to a .npy file."""
        if self.tilemap_generator.tile_map is None:
            print("DEBUG: save_tilemap_data - No tilemap data to save (tile_map is None).") # Debug print
            return

        if self.input_map_file_path:
            base_name = os.path.splitext(os.path.basename(self.input_map_file_path))[0]
            output_dir = os.path.dirname(self.input_map_file_path)
            output_npy_path = os.path.join(output_dir, f"{base_name}_tilemap.npy")
            print(f"DEBUG: save_tilemap_data - Attempting to save to: {output_npy_path}") # Debug print
            try:
                np.save(output_npy_path, self.tilemap_generator.tile_map)
                print(f"Tilemap data saved to {output_npy_path}")
            except Exception as e:
                print(f"Error saving tilemap data to {output_npy_path}: {e}") # Enhanced error message
        else:
            print("DEBUG: save_tilemap_data - Cannot determine output file name, input map file path is not set.") # Debug print

    def save_tilemap_image(self):
        """Renders the current QGraphicsScene to an image and saves it as PNG."""
        if self.visualization_widget.scene_obj.sceneRect().isEmpty():
            print("DEBUG: save_tilemap_image - Scene is empty, cannot save image.") # Debug print
            return
        
        if self.input_map_file_path:
            base_name = os.path.splitext(os.path.basename(self.input_map_file_path))[0]
            output_dir = os.path.dirname(self.input_map_file_path)
            output_png_path = os.path.join(output_dir, f"{base_name}_tilemap.png")
            print(f"DEBUG: save_tilemap_image - Attempting to save to: {output_png_path}") # Debug print

            # Create an image to render the scene onto
            # It's better to get map_width and map_depth from the tile_map itself, 
            # as map_width_tiles and map_depth_tiles could be 0 if no brushes loaded.
            if self.tilemap_generator.tile_map is None or self.tilemap_generator.tile_map.size == 0:
                 print(f"DEBUG: save_tilemap_image - Tilemap is empty or None. Cannot save image.")
                 return
            
            map_depth, map_width = self.tilemap_generator.tile_map.shape # Get dimensions from the final tile_map

            # Calculate pixel dimensions for the image
            image_width_px = int(map_width * TILE_SIZE)
            image_height_px = int(map_depth * TILE_SIZE)

            if image_width_px <= 0 or image_height_px <= 0:
                print(f"DEBUG: save_tilemap_image - Calculated image dimensions are invalid: {image_width_px}x{image_height_px}. Cannot save image.")
                return

            image = QImage(image_width_px, image_height_px, QImage.Format_ARGB32)
            image.fill(Qt.white) # Fill with white background

            painter = QPainter(image)
            # Render the simple wall/floor grid for the PNG
            tile_map = self.tilemap_generator.tile_map
            if tile_map is not None:
                for z_idx in range(map_depth):
                    for x_idx in range(map_width):
                        tile_type = tile_map[z_idx, x_idx]
                        fill_color = QColor(255, 255, 255) # White for floor
                        if tile_type == WALL_TILE:
                            fill_color = QColor(90, 90, 90) # Dark grey for wall
                        
                        painter.fillRect(
                            int(x_idx * TILE_SIZE), int(z_idx * TILE_SIZE),
                            int(TILE_SIZE), int(TILE_SIZE),
                            QBrush(fill_color)
                        )
                        # Draw grid lines
                        painter.setPen(QPen(QColor(200, 200, 200), 1)) # Light grey grid lines
                        painter.drawRect(
                            int(x_idx * TILE_SIZE), int(z_idx * TILE_SIZE),
                            int(TILE_SIZE), int(TILE_SIZE)
                        )
            
            painter.end()

            try:
                image.save(output_png_path)
                print(f"Tilemap image saved to {output_png_path}")
            except Exception as e:
                print(f"Error saving tilemap image to {output_png_path}: {e}") # Enhanced error message
        else:
            print("DEBUG: save_tilemap_image - Cannot determine output file name, input map file path is not set.") # Debug print


if __name__ == '__main__':
    app = QApplication(sys.argv)
    
    map_file = None
    if len(sys.argv) > 1:
        map_file = sys.argv[1] # Get the file path from the command-line arguments

    window = TilemapVisualizationWindow(map_file_path=map_file)
    window.show()
    sys.exit(app.exec_())
