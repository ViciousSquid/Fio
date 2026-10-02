# level_editor.py

import tkinter as tk
from tkinter import ttk, filedialog, simpledialog, colorchooser
import tkinter.font as tkfont
import json
import math
import os
import copy
import random
import threading
import queue
from PIL import Image, ImageTk, ImageDraw
import pygame
import numpy as np
from game_engine import GameView, WALL_TILE, FLOOR_TILE, PORTAL_A_TILE, PORTAL_B_TILE, TILE_SIZE, FLOOR_HEIGHT
from generate_level import LevelGeneratorA, LevelGeneratorB
import sys
import tempfile
import subprocess
from editor.AssetBrowser import AssetBrowser
from editor.SettingsWindow import SettingsWindow
from editor.sidebar import Sidebar
import configparser


# --- Default editor constants ---
INITIAL_GRID_WIDTH, INITIAL_GRID_HEIGHT = 80, 50
TILE_DISPLAY_SIZE = 15
DEFAULT_FONT_SIZE = 10

# --- Tilemap colors for the editor ---
TILE_COLORS = {
    WALL_TILE: "#2c3e50",
    FLOOR_TILE: "#bdc3c7",
    PORTAL_A_TILE: "#3498db",
    PORTAL_B_TILE: "#e67e22"
}

class LevelEditor:

    WALL_TILE = 0
    FLOOR_TILE = 1
    TEXTURE_BRUSH = 2
    PORTAL_A_TILE = 3
    PORTAL_B_TILE = 4

    def __init__(self, root):
        self.root = root
        self.root.title("Editor")
        self.root.minsize(1024, 768)

        # --- Core Editor State ---
        self.game_view = None
        self.grid_width = INITIAL_GRID_WIDTH
        self.grid_height = INITIAL_GRID_HEIGHT
        self.tile_map = np.full((self.grid_height, self.grid_width), self.WALL_TILE, dtype=int)
        self.player_start = {'x': 100, 'y': 100, 'angle': 0}
        self.lights = []
        self.objects = []
        self.textures = {'default': None}
        self.brush_var = tk.IntVar(value=self.FLOOR_TILE) 
        self.current_brush = self.brush_var.get()

        # Texture and Selection Management
        self.wall_textures = {} 
        self.active_texture_name = None
        self.selection_rect_id = None
        self.selection_start_pos = None
        self.selected_tiles = []

        # --- Selection and UI State ---
        self.selected_light_index = None
        self.selected_object_index = None
        self.dragging_light = False
        self.dragging_object = False

        # --- History Management ---
        self.history = []
        self.current_state_index = -1

        # --- Configuration ---
        self.config = configparser.ConfigParser()
        # ** FIX: Initialize physics_enabled with a default before loading config **
        self.physics_enabled = True 
        self.load_config()
        self.show_game_fps = self.config.getboolean('Editor', 'ShowGameFPS', fallback=False)
        self.physics_enabled = self.config.getboolean('Editor', 'PhysicsEnabled', fallback=self.physics_enabled)
        
        default_font_size = self.config.getint('Editor', 'FontSize', fallback=DEFAULT_FONT_SIZE)
        self.default_font = tkfont.nametofont("TkDefaultFont")
        self.update_font_size(default_font_size)

        # --- UI Setup ---
        main_frame = tk.Frame(root)
        main_frame.pack(expand=True, fill=tk.BOTH)

        self.canvas = tk.Canvas(main_frame, bg='grey20')
        self.canvas.pack(side=tk.RIGHT, expand=True, fill=tk.BOTH)
        
        self.sidebar = Sidebar(main_frame, self, self.brush_var) 
        self.controls_frame = self.sidebar.frame

        # --- Game Engine View ---
        self.game_view = None

        # --- Child Windows & Dialogs ---
        self.settings_window = SettingsWindow(self.root, self, default_font_size)
        self.settings_window.show_fps_var.set(self.show_game_fps)
        self.settings_window.physics_var.set(self.physics_enabled)
        self.settings_window.set_maps_path(os.path.abspath("maps"))
        
        # --- Final Setup ---
        self._setup_menubar()
        self._setup_bindings()

        self.redraw_canvas()
        self._setup_main_menu_overlay()
        self.disable_ui_for_overlay()

        self.save_state("Initial state")

    def load_config(self):
        if os.path.exists('config.ini'):
            self.config.read('config.ini')
            self.physics_enabled = self.config.getboolean('Settings', 'physics', fallback=True)
        else:
            self.save_config()

    def save_config(self):
        if 'Settings' not in self.config:
            self.config['Settings'] = {}
        self.config['Settings']['physics'] = str(self.physics_enabled)
        with open('config.ini', 'w') as configfile:
            self.config.write(configfile)

    def start_game_view(self):
        """Initializes and runs the 3D game view."""
        if self.game_view and self.game_view.running:
            print("Game view is already running.")
            return

        print("Starting game view...")
        
        self.canvas.pack_forget()
        
        # Pass the wall_textures data to the engine
        self.game_view = GameView(
            self.root,
            self.tile_map,
            self.player_start,
            self.lights,
            self.objects,
            self.textures,
            self.wall_textures,
            self.physics_enabled,
            self.show_game_fps
        )
        
        self.game_view.on_close(self.stop_game_view)
        self.game_view.run()

    def stop_game_view(self):
        """Stops the game view and returns to the editor."""
        print("Stopping game view...")
        self.game_view = None
        # Bring back the 2D editor canvas
        self.canvas.pack(side="right", expand=True, fill="both")
        self.redraw_canvas()

    def set_physics_enabled(self, enabled):
        self.physics_enabled = enabled
        self.save_config()

    def _setup_main_menu_overlay(self):
        self.main_menu_overlay = tk.Frame(self.root, bg='#3b3b3b')
        self.main_menu_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        liminal_frame = tk.Frame(self.main_menu_overlay, bg='#3b3b3b')
        liminal_frame.pack(pady=(50, 20))

        liminal_canvas = tk.Canvas(liminal_frame, bg='#3b3b3b', highlightthickness=0)
        liminal_canvas.pack()

        liminal_font = tkfont.Font(family="Courier New", size=48, weight="bold")
        text_width = liminal_font.measure("Backrooms")
        text_height = liminal_font.metrics('linespace')

        padding_x = 30
        padding_y = 15
        
        canvas_width = text_width + 2 * padding_x
        canvas_height = text_height + 2 * padding_y

        liminal_canvas.config(width=canvas_width, height=canvas_height)

        liminal_canvas.create_rectangle(
            0, 0, canvas_width, canvas_height,
            fill="#454545", outline="#FFFFFF", width=2
        )
        
        liminal_canvas.create_text(
            canvas_width / 2, canvas_height / 2,
            text="Backrooms",
            font=liminal_font,
            fill="white",
            anchor=tk.CENTER
        )

        button_container = tk.Frame(self.main_menu_overlay, bg='#3b3b3b')
        button_container.pack(expand=True, fill='x', padx=20)

        button_font = ('Arial', 20) 

        play_frame = tk.Frame(button_container, height=100)
        play_frame.pack(fill=tk.X, pady=15, padx=100)
        play_frame.pack_propagate(False)
        play_button = tk.Button(play_frame, text='Play a map',
                                 command=self.open_map_launcher,
                                 bg='#7f9e46', fg='white', font=button_font)
        play_button.pack(expand=True, fill=tk.BOTH)

        create_frame = tk.Frame(button_container, height=100)
        create_frame.pack(fill=tk.X, pady=15, padx=100)
        create_frame.pack_propagate(False)
        create_button = tk.Button(create_frame, text='Editor',
                                 command=self.hide_overlay_and_enable_ui,
                                 bg='#97a5ca', fg='white', font=button_font)
        create_button.pack(expand=True, fill=tk.BOTH)

        self.hidden_button_frame = tk.Frame(button_container, height=100)
        self.hidden_button_frame.pack(fill=tk.X, pady=15, padx=100)
        self.hidden_button_frame.pack_propagate(False)
        self.hidden_button = tk.Button(self.hidden_button_frame, text='Load a map',
                                         bg='#550000', fg='white', font=button_font,
                                         command=lambda: [self.load_level(), self.hide_overlay_and_enable_ui()])
        self.hidden_button.pack(expand=True, fill=tk.BOTH)

    def _setup_menubar(self):
        self.menubar = tk.Menu(self.root)
        self.root.config(menu=self.menubar)

        self.file_menu = tk.Menu(self.menubar, tearoff=0)
        self.file_menu.add_command(label="New", command=self.clear_canvas)
        self.file_menu.add_command(label="Open...", command=self.load_level)
        self.file_menu.add_command(label="Save", command=self.save_level)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.on_closing)
        self.menubar.add_cascade(label="File", menu=self.file_menu)

        self.edit_menu = tk.Menu(self.menubar, tearoff=0)
        self.edit_menu.add_command(label="Undo", command=self.undo, accelerator="Ctrl+Z")
        self.edit_menu.add_command(label="Redo", command=self.redo, accelerator="Ctrl+Y")
        self.menubar.add_cascade(label="Edit", menu=self.edit_menu)
        
        self.view_menu = tk.Menu(self.menubar, tearoff=0)
        self.view_menu.add_command(label="Settings", command=self.settings_window.toggle_visibility)
        self.menubar.add_cascade(label="View", menu=self.view_menu)
        
        self.game_menu = tk.Menu(self.menubar, tearoff=0)
        self.game_menu.add_command(label="Launch Map", command=self.open_map_launcher)
        self.menubar.add_cascade(label="Game", menu=self.game_menu)

    def set_active_texture(self, texture_path, texture_name):
        """Sets the current brush to texture mode and stores the selected texture."""
        self.current_brush = self.TEXTURE_BRUSH
        self.active_texture_name = texture_name
        print(f"Brush changed to: Texture Paint ({self.active_texture_name})")

    def on_canvas_press(self, event):
        """Handles mouse clicks on the 2D canvas."""
        col = int(event.x / TILE_DISPLAY_SIZE)
        row = int(event.y / TILE_DISPLAY_SIZE)

        # Handle texture painting first if that brush is active
        if self.current_brush == self.TEXTURE_BRUSH:
            if self.tile_map[row][col] == self.WALL_TILE and self.active_texture_name:
                x_in_tile = (event.x % TILE_DISPLAY_SIZE) / TILE_DISPLAY_SIZE
                y_in_tile = (event.y % TILE_DISPLAY_SIZE) / TILE_DISPLAY_SIZE

                face = ''
                if y_in_tile < 0.25: face = 'N'
                elif y_in_tile > 0.75: face = 'S'
                elif x_in_tile < 0.25: face = 'W'
                elif x_in_tile > 0.75: face = 'E'

                if face:
                    if (row, col) not in self.wall_textures:
                        self.wall_textures[(row, col)] = {}

                    # Toggle texture on/off
                    if self.wall_textures[(row, col)].get(face) == self.active_texture_name:
                        self.wall_textures[(row, col)][face] = None
                        print(f"Removed texture from wall ({row}, {col}) face {face}")
                    else:
                        self.wall_textures[(row, col)][face] = self.active_texture_name
                        print(f"Applied '{self.active_texture_name}' to wall ({row}, {col}) face {face}")

                    self.redraw_canvas()
                    self.save_state("Apply Texture")
            return # Stop further processing if we are in texture mode

        # Handle standard tile painting
        if 0 <= row < self.grid_height and 0 <= col < self.grid_width:
            # Check for fill command (Ctrl+Click)
            if event.state & 0x0004:
                self.paint_mode = "fill"
                self.fill_area(row, col, self.current_brush)
            else:
                self.paint_mode = "brush"
                # This is the corrected call
                self.paint_tile(row, col)

            # Save state after painting or filling
            self.save_state(f"Paint {self.paint_mode}")

    def start_selection(self, event):
        """Records the starting position of a selection drag."""
        # Only start selection if in texture mode
        if self.current_brush == self.TEXTURE_BRUSH:
            self.selection_start_pos = (event.x, event.y)
            self.clear_selection()

    def update_selection(self, event):
        """Draws and updates the selection rectangle on the canvas."""
        if not self.selection_start_pos:
            return

        # Delete the old rectangle
        if self.selection_rect_id:
            self.canvas.delete(self.selection_rect_id)
        
        x1, y1 = self.selection_start_pos
        x2, y2 = event.x, event.y
        
        # Draw the new one
        self.selection_rect_id = self.canvas.create_rectangle(
            x1, y1, x2, y2, outline="#3498db", width=2, dash=(5, 5)
        )

    def end_selection(self, event):
        """Finalizes the selection and identifies all wall tiles within it."""
        if not self.selection_start_pos:
            return

        x_start, y_start = self.selection_start_pos
        x_end, y_end = event.x, event.y

        x1, x2 = min(x_start, x_end), max(x_start, x_end)
        y1, y2 = min(y_start, y_end), max(y_start, y_end)

        start_col, end_col = int(x1 / TILE_DISPLAY_SIZE), int(x2 / TILE_DISPLAY_SIZE)
        start_row, end_row = int(y1 / TILE_DISPLAY_SIZE), int(y2 / TILE_DISPLAY_SIZE)

        self.selected_tiles = []
        for r in range(start_row, end_row + 1):
            for c in range(start_col, end_col + 1):
                if 0 <= r < self.grid_height and 0 <= c < self.grid_width:
                    if self.tile_map[r, c] == WALL_TILE:
                        self.selected_tiles.append((r, c))
        
        self.selection_start_pos = None
        self.redraw_canvas()

    def apply_texture_to_selection(self):
        """Applies the active texture to all faces of the selected wall tiles."""
        if not self.selected_tiles or not self.active_texture_name:
            return
        
        self.save_state("Apply Texture to Selection")
        for r, c in self.selected_tiles:
            if (r, c) not in self.wall_textures:
                self.wall_textures[(r, c)] = {}
            for face in ['N', 'S', 'E', 'W']:
                self.wall_textures[(r, c)][face] = self.active_texture_name
        
        self.clear_selection()
        self.redraw_canvas()

    def clear_selection(self):
        """Clears the current tile selection."""
        self.canvas.delete(self.selection_rect_id)
        self.selection_rect_id = None
        self.selected_tiles = []
        self.redraw_canvas()


    def _setup_bindings(self):
        self._canvas_mouse_drag_func = self.mouse_drag
        self._canvas_mouse_down_func = self.mouse_down
        self._canvas_mouse_up_func = self.mouse_up
        self._canvas_button3_func = lambda e: self.context_menu.post(e.x_root, e.y_root)
        self._canvas_set_player_pos_func = self.set_player_pos
        self._canvas_set_player_dir_func = self.set_player_dir

        self.canvas.bind("<B1-Motion>", self._canvas_mouse_drag_func)
        self.canvas.bind("<Button-1>", self._canvas_mouse_down_func)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_mouse_up_func)
        self.canvas.bind("<Button-3>", self._canvas_button3_func)
        self.context_menu = tk.Menu(self.canvas, tearoff=0)
        self.context_menu.add_command(label="Add Light", command=self.add_light_at_cursor)
        self.context_menu.add_command(label="Model", command=self.add_object_at_cursor)
        self.canvas.bind("<Control-Button-1>", self._canvas_set_player_pos_func)
        self.canvas.bind("<B3-Motion>", self._canvas_set_player_dir_func)
        
        # Selection and texture bindings
        self.canvas.bind("<Motion>", self.draw_texture_hover_indicator)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete('texture_hover_indicator'))
        self.canvas.bind("<Shift-Button-1>", self.start_selection)
        self.canvas.bind("<Shift-B1-Motion>", self.update_selection)
        self.canvas.bind("<Shift-ButtonRelease-1>", self.end_selection)
        self.root.bind("<Return>", lambda e: self.apply_texture_to_selection())
        self.root.bind("<Escape>", lambda e: self.clear_selection())

        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def update_font_size(self, size):
        self.default_font.configure(size=size)
        style = ttk.Style()
        style.configure("TButton", font=(self.default_font.actual()['family'], size))
        style.configure("TLabel", font=(self.default_font.actual()['family'], size))
        style.configure("TRadiobutton", font=(self.default_font.actual()['family'], size))
        style.configure("TNotebook.Tab", font=(self.default_font.actual()['family'], size))

    def toggle_game_fps(self, show_fps_status):
        self.show_game_fps = show_fps_status
        if self.game_view:
            self.game_view.set_show_fps(self.show_game_fps)

    def check_player_start_validity(self):
        if not self.player_start or 'x' not in self.player_start or 'y' not in self.player_start:
            self.sidebar.play_button.pack_forget()
            return
        
        col = int(self.player_start['x'] / TILE_SIZE)
        row = int(self.player_start['y'] / TILE_SIZE)

        if not (0 < col < self.grid_width - 1 and 0 < row < self.grid_height - 1):
            self.sidebar.play_button.pack_forget()
            return

        floor_count = 0
        for r in range(row - 1, row + 2):
            for c in range(col - 1, col + 2):
                if self.tile_map[r, c] == FLOOR_TILE:
                    floor_count += 1
        
        if floor_count >= 4:
            self.play_button.pack(fill=tk.X, pady=2)
        else:
            self.sidebar.play_button.pack_forget()

    def open_generation_settings(self):
        settings_win = tk.Toplevel(self.root)
        settings_win.title("Generation Settings")
        settings_win.transient(self.root)
        settings_win.grab_set()

        frame = tk.Frame(settings_win, padx=15, pady=15)
        frame.pack(expand=True, fill=tk.BOTH)

        controls_frame = tk.Frame(frame)
        controls_frame.pack(fill=tk.X)

        dim_frame = tk.Frame(controls_frame)
        dim_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(dim_frame, text="Width:").pack(side=tk.LEFT)
        width_spinbox = tk.Spinbox(dim_frame, from_=50, to=500, width=5)
        width_spinbox.delete(0, "end")
        width_spinbox.insert(0, self.grid_width)
        width_spinbox.pack(side=tk.LEFT, padx=(0, 15))
        tk.Label(dim_frame, text="Height:").pack(side=tk.LEFT)
        height_spinbox = tk.Spinbox(dim_frame, from_=50, to=500, width=5)
        height_spinbox.delete(0, "end")
        height_spinbox.insert(0, self.grid_height)
        height_spinbox.pack(side=tk.LEFT)

        tk.Label(controls_frame, text="\nGenerator Type").pack(anchor=tk.W)
        generator_var = tk.StringVar(value='Generator A')
        generator_options = ['Generator A', 'Generator B']
        generator_menu = ttk.Combobox(controls_frame, textvariable=generator_var, values=generator_options, state="readonly")
        generator_menu.pack(fill=tk.X, pady=(0, 10))

        tk.Label(controls_frame, text="Seed (Optional)").pack(anchor=tk.W)
        seed_entry = tk.Entry(controls_frame)
        seed_entry.pack(fill=tk.X)

        progress_frame = tk.Frame(frame, pady=10)
        progress_frame.pack(fill=tk.X, side=tk.BOTTOM)
        progress_bar = ttk.Progressbar(progress_frame, orient='horizontal', mode='determinate', length=250)
        progress_bar.pack(fill=tk.X, pady=5)

        btn_frame = tk.Frame(progress_frame)
        btn_frame.pack(fill=tk.X)

        generate_btn = tk.Button(btn_frame, text="Generate", bg="#a5d6a7", command=lambda: self._start_generation_thread(
            settings_win, [width_spinbox, height_spinbox, seed_entry, generate_btn, generator_var], progress_bar
        ))
        generate_btn.pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="Cancel", command=settings_win.destroy).pack(side=tk.RIGHT)

    def _start_generation_thread(self, window, widgets, progress_bar):
        width_spin, height_spin, seed_entry, gen_btn, generator_var = widgets

        for widget in widgets[:-1]:
            widget.config(state='disabled')

        try:
            width, height = int(width_spin.get()), int(height_spin.get())
        except ValueError:
            width, height = 100, 60

        params = {
            "width": width, "height": height,
            "seed": int(seed_entry.get()) if seed_entry.get().isdigit() else None,
            "generator_type": generator_var.get()
        }

        self.progress_queue = queue.Queue()
        thread = threading.Thread(target=self._generation_thread_worker, args=(params, self.progress_queue))
        thread.start()

        window.after(100, self._check_generation_queue, window, progress_bar)

    def _generation_thread_worker(self, params, q):
        if params["seed"] is not None:
            random.seed(params["seed"])
            np.random.seed(params["seed"])

        if params["generator_type"] == 'Generator B':
            generator = LevelGeneratorB(width=params["width"], height=params["height"])
        else:  # Default to genA
            generator = LevelGeneratorA(width=params["width"], height=params["height"])
        
        tile_map = generator.generate()
        q.put(('done', tile_map))

    def _check_generation_queue(self, window, progress_bar):
        try:
            message = self.progress_queue.get_nowait()
            if isinstance(message, tuple) and message[0] == 'done':
                tile_map = message[1]
                self._finish_generation(tile_map)
                window.destroy()
            else:
                progress_bar['value'] = message
                window.after(100, self._check_generation_queue, window, progress_bar)
        except queue.Empty:
            window.after(100, self._check_generation_queue, window, progress_bar)

    def _finish_generation(self, tile_map):
        self.save_state()
        self.grid_height, self.grid_width = tile_map.shape
        self.tile_map = tile_map
        self.lights = []
        self.objects = []
        self._find_valid_player_start()
        self.canvas.config(width=self.grid_width * TILE_DISPLAY_SIZE, height=self.grid_height * TILE_DISPLAY_SIZE)
        self.redraw_canvas()
        self.sync_3d_view()
        self.update_light_list()
        self.update_object_list()
        self.asset_browser.repopulate_from_data(self.textures)
        self.check_player_start_validity()
        
    def _find_valid_player_start(self):
        valid_starts = []
        clear_radius = 4
        for r in range(clear_radius, self.grid_height - clear_radius):
            for c in range(clear_radius, self.grid_width - clear_radius):
                if self.tile_map[r, c] == FLOOR_TILE:
                    area = self.tile_map[r-clear_radius : r+clear_radius+1, c-clear_radius : c+clear_radius+1]
                    if np.all(area == FLOOR_TILE):
                        valid_starts.append((c, r))
        if valid_starts:
            c, r = random.choice(valid_starts)
            self.player_start = {'x': (c + 0.5) * TILE_SIZE, 'y': (r + 0.5) * TILE_SIZE, 'angle': 0}
        else:
            floor_tiles = np.argwhere(self.tile_map == FLOOR_TILE)
            if len(floor_tiles) > 0:
                r, c = floor_tiles[0]
                self.player_start = {'x': (c + 0.5) * TILE_SIZE, 'y': (r + 0.5) * TILE_SIZE, 'angle': 0}
            else:
                self.player_start = {'x': 100, 'y': 100, 'angle': 0}

    def select_brush(self):
        self.current_brush = self.brush_var.get()
        print(f"Brush changed to: {self.current_brush}")

    def paint_tile(self, row, col):
        if 0 <= col < self.grid_width and 0 <= row < self.grid_height:
            if self.tile_map[row, col] != self.current_brush:
                self.save_state("Paint Tile")
                self.tile_map[row, col] = self.current_brush
                self.redraw_canvas()
                self.sync_3d_view()
                self.check_player_start_validity()

    def draw_texture_hover_indicator(self, event):
        # First, remove any existing hover indicator
        self.canvas.delete('texture_hover_indicator')

        # Only show the indicator if the texture brush is active
        if self.current_brush != self.TEXTURE_BRUSH:
            return

        col = int(event.x / TILE_DISPLAY_SIZE)
        row = int(event.y / TILE_DISPLAY_SIZE)

        # Check if the cursor is within the grid and over a wall tile
        if 0 <= row < self.grid_height and 0 <= col < self.grid_width and self.tile_map[row][col] == WALL_TILE:
            x1, y1 = col * TILE_DISPLAY_SIZE, row * TILE_DISPLAY_SIZE
            x2, y2 = x1 + TILE_DISPLAY_SIZE, y1 + TILE_DISPLAY_SIZE
            
            x_in_tile = (event.x % TILE_DISPLAY_SIZE) / TILE_DISPLAY_SIZE
            y_in_tile = (event.y % TILE_DISPLAY_SIZE) / TILE_DISPLAY_SIZE

            indicator_color = "#3498db"  # A distinct blue for the preview
            indicator_width = 2
            face_to_draw = None

            if y_in_tile < 0.25: face_to_draw = 'N'
            elif y_in_tile > 0.75: face_to_draw = 'S'
            elif x_in_tile < 0.25: face_to_draw = 'W'
            elif x_in_tile > 0.75: face_to_draw = 'E'

            if face_to_draw == 'N':
                self.canvas.create_line(x1 + 1, y1 + 1, x2 - 1, y1 + 1, fill=indicator_color, width=indicator_width, tags='texture_hover_indicator')
            elif face_to_draw == 'S':
                self.canvas.create_line(x1 + 1, y2 - 1, x2 - 1, y2 - 1, fill=indicator_color, width=indicator_width, tags='texture_hover_indicator')
            elif face_to_draw == 'W':
                self.canvas.create_line(x1 + 1, y1 + 1, x1 + 1, y2 - 1, fill=indicator_color, width=indicator_width, tags='texture_hover_indicator')
            elif face_to_draw == 'E':
                self.canvas.create_line(x2 - 1, y1 + 1, x2 - 1, y2 - 1, fill=indicator_color, width=indicator_width, tags='texture_hover_indicator')

    def redraw_canvas(self):
        self.canvas.delete("all")
        selected_set = set(self.selected_tiles)

        for r in range(self.grid_height):
            for c in range(self.grid_width):
                x1, y1 = c * TILE_DISPLAY_SIZE, r * TILE_DISPLAY_SIZE
                x2, y2 = x1 + TILE_DISPLAY_SIZE, y1 + TILE_DISPLAY_SIZE
                
                tile_type = self.tile_map[r, c]
                color = TILE_COLORS.get(tile_type, "black")
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="#555")

                # 1. Draw PERMANENT highlight for any textured wall
                if tile_type == WALL_TILE and (r, c) in self.wall_textures:
                    # Check if any face has a texture before highlighting
                    if any(self.wall_textures[(r, c)].values()):
                        self.canvas.create_rectangle(
                            x1, y1, x2, y2, fill="#f1c40f", stipple="gray12", outline=""
                        )

                # 2. Draw TEMPORARY highlight for the current selection
                if (r, c) in selected_set:
                    self.canvas.create_rectangle(
                        x1, y1, x2, y2, fill="#3498db", stipple="gray25", outline=""
                    )
        
        self.draw_objects()
        self.draw_lights()
        if self.player_start: self.draw_player_start()
        self.draw_realtime_player_indicator()

    def play_level(self):
        maps_dir = "maps"
        if not os.path.exists(maps_dir):
            os.makedirs(maps_dir)
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json', dir=maps_dir) as temp_file:
            level_data = {
                "grid_width": self.grid_width,
                "grid_height": self.grid_height,
                "tile_map": self.tile_map.tolist(),
                "player_start": self.player_start,
                "lights": self.lights,
                "objects": self.objects,
                "textures": self.textures
            }
            json.dump(level_data, temp_file)
            temp_file_path = temp_file.name

        python_executable = sys.executable
        command = [python_executable, "game_engine.py", temp_file_path]
        if self.show_game_fps:
            command.append("--show-fps")
        subprocess.Popen(command)

    def save_state(self, description=""):
        """Saves the current state, trimming the history for a correct undo/redo chain."""
        
        # Trim the history from the current point onwards
        if self.current_state_index < len(self.history) - 1:
            self.history = self.history[:self.current_state_index + 1]

        # Create the new state object
        state = {
            'tile_map': np.copy(self.tile_map),
            'player_start': self.player_start.copy(),
            'lights': [light.copy() for light in self.lights],
            'objects': [obj.copy() for obj in self.objects],
            'description': description if description else f"Action {len(self.history)}"
        }
        
        # Add the new state to the history
        self.history.append(state)
        self.current_state_index = len(self.history) - 1

        # Update the UI
        self.sidebar.update_history_list([s['description'] for s in self.history])


    def on_history_select(self, event):
        if not self.sidebar.history_listbox.curselection():
            return
        selected_index = self.sidebar.history_listbox.curselection()[0]
        if selected_index != self.current_state_index:
            self.load_state_from_history(selected_index)

    def undo(self, event=None):
        if self.current_state_index > 0:
            self.load_state_from_history(self.current_state_index - 1)
        return "break"
    
    def set_selected_light_color(self, hex_color):
        """Applies the chosen color to the currently selected light."""
        print(f"Setting selected light color to: {hex_color}")
        # Add your logic here to find the selected light and change its color.
        # This will likely involve calling a method on your self.lights_manager.
        
        # After changing the color, add it to the history
        #self.add_history_item(f"Set light color to {hex_color}")

    def redo(self, event=None):
        if self.current_state_index < len(self.history) - 1:
            self.load_state_from_history(self.current_state_index + 1)
        return "break"
    
    def load_state_from_history(self, index):
        """Loads a specific state from the history list by its index."""
        self.current_state_index = index
        state = self.history[self.current_state_index]
        self.load_state(state) # Assumes you have a self.load_state(state) method
        self.sidebar.update_history_list([s['description'] for s in self.history])
        print(f"Loaded history state {self.current_state_index}: {state['description']}")
            
    def load_state(self, state):
        self.grid_width = state['grid_width']
        self.grid_height = state['grid_height']
        self.canvas.config(width=self.grid_width * TILE_DISPLAY_SIZE, height=self.grid_height * TILE_DISPLAY_SIZE)
        self.tile_map = state['tile_map'].copy()
        self.player_start = state['player_start'].copy()
        self.lights = copy.deepcopy(state['lights'])
        self.objects = copy.deepcopy(state.get('objects', []))
        self.textures = state.get('textures', {'default':None}).copy()
        self.wall_textures = copy.deepcopy(state.get('wall_textures', {}))

        self.redraw_canvas()
        self.sync_3d_view()
        self.update_light_list()
        # You may need to add self.update_object_list() if that exists
        self.sidebar.asset_browser.repopulate_from_data(self.textures)
        self.check_player_start_validity()
        self.deselect_all()

    def save_level(self):
        maps_dir = "maps"
        if not os.path.exists(maps_dir):
            os.makedirs(maps_dir)
        filepath = filedialog.asksaveasfilename(
            initialdir=maps_dir,
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")]
        )
        if filepath:
            data = {
                "grid_width": self.grid_width,
                "grid_height": self.grid_height,
                "tile_map": self.tile_map.tolist(),
                "player_start": self.player_start,
                "lights": self.lights,
                "objects": self.objects,
                "textures": self.textures,
                "wall_textures": {str(k): v for k, v in self.wall_textures.items()}
            }
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)

    def load_level(self):
        filepath = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if not filepath: return
        with open(filepath, 'r') as f: data = json.load(f)

        if "tile_map" in data:
            self.save_state("Load Level")
            self.tile_map = np.array(data['tile_map'])
            self.grid_height, self.grid_width = self.tile_map.shape
            self.canvas.config(width=self.grid_width * TILE_DISPLAY_SIZE, height=self.grid_height * TILE_DISPLAY_SIZE)
            self.player_start = data.get('player_start', {'x': 100, 'y': 100, 'angle': 0})
            self.objects = data.get("objects", [])
            self.textures = data.get("textures", {'default': None})
            
            self.lights = data.get('lights', [])
            # Backwards compatibility for old map files: Update old light objects that are missing the intensity key
            for light in self.lights:
                if 'intensity' not in light:
                    light['intensity'] = 500.0 # Assign a default intensity

            self.wall_textures = {}
            wall_tex_data = data.get("wall_textures", {})
            for k, v in wall_tex_data.items():
                self.wall_textures[eval(k)] = v
            
            self.redraw_canvas()
            self.sync_3d_view()
            self.update_light_list()
            self.sidebar.asset_browser.repopulate_from_data(self.textures)
            self.check_player_start_validity()
            self.deselect_all()

    def clear_canvas(self):
        self.save_state()
        self.tile_map = np.full((self.grid_height, self.grid_width), WALL_TILE, dtype=int)
        self.lights = []
        self.objects = []
        self.player_start = {'x': 100, 'y': 100, 'angle': 0}
        self.redraw_canvas()
        self.sync_3d_view()
        self.update_light_list()
        self.update_object_list()
        self.check_player_start_validity()
        self.deselect_all()

    def add_light_at_cursor(self):
        # Get cursor position relative to the canvas
        canvas_x = self.canvas.canvasx(self.root.winfo_pointerx() - self.canvas.winfo_rootx())
        canvas_y = self.canvas.canvasy(self.root.winfo_pointery() - self.canvas.winfo_rooty())
        
        # Convert canvas coordinates to game coordinates
        game_x = (canvas_x / TILE_DISPLAY_SIZE) * TILE_SIZE
        game_z = (canvas_y / TILE_DISPLAY_SIZE) * TILE_SIZE
        
        # Use the new generic add_light method
        self.add_light(position=(game_x, game_z))

    def add_light(self, position=None):
        self.save_state("Add Light")
        if position:
            game_x, game_z = position
        else:
            # Default to center of canvas if no position is provided
            game_x = (self.canvas.winfo_width() / 2 / TILE_DISPLAY_SIZE) * TILE_SIZE
            game_z = (self.canvas.winfo_height() / 2 / TILE_DISPLAY_SIZE) * TILE_SIZE

        new_light = {
            'pos': [game_x, 150.0, game_z], # Default Y position and position
            'color': [1.0, 1.0, 1.0],      # Default white color
            'intensity': 500                # Default intensity
        }
        self.lights.append(new_light)
        self.update_light_list()
        self.redraw_canvas()
        # Select the new light so the user can see and edit it immediately
        self.select_light(len(self.lights) - 1)

    def add_object_at_cursor(self):
        x, y = self.root.winfo_pointerx() - self.canvas.winfo_rootx(), self.root.winfo_pointery() - self.canvas.winfo_rooty()
        self._add_object_logic(position=(x,y))

    def add_object(self):
        self._add_object_logic()
    
    def _add_object_logic(self, position=None):
        filepath = filedialog.askopenfilename(filetypes=[("OBJ files", "*.obj")])
        if not filepath: return
        
        rel_path = os.path.relpath(filepath, os.getcwd())

        if position:
            x, y = position
        else:
            x, y = self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2
        
        game_x = round((x / TILE_DISPLAY_SIZE)) * TILE_SIZE
        game_z = round((y / TILE_DISPLAY_SIZE)) * TILE_SIZE
        
        self.save_state()
        self.objects.append({
            "path": rel_path.replace("\\", "/"),
            "pos": [game_x, FLOOR_HEIGHT, game_z],
            "scale": [20, 20, 20]
        })
        self.update_object_list()
        self.sync_3d_view()
        self.redraw_canvas()
        self.select_object(len(self.objects) - 1)

    def draw_lights(self):
        self.canvas.delete('light')
        for i, light in enumerate(self.lights):
            # Convert game position to canvas coordinates for the light's center
            canvas_x = (light['pos'][0] / TILE_SIZE) * TILE_DISPLAY_SIZE
            canvas_y = (light['pos'][2] / TILE_SIZE) * TILE_DISPLAY_SIZE
            
            # Draw the light's core
            fill_color = '#%02x%02x%02x' % (int(light['color'][0]*255), int(light['color'][1]*255), int(light['color'][2]*255))
            self.canvas.create_oval(canvas_x - 5, canvas_y - 5, canvas_x + 5, canvas_y + 5, fill=fill_color, tags=('light', f'light_{i}'))

            # If the light is selected, draw the selection indicator and radius
            if i == self.selected_light_index:
                # 1. Draw a white selection box around the core
                self.canvas.create_rectangle(
                    canvas_x - 7, canvas_y - 7, canvas_x + 7, canvas_y + 7, 
                    outline='white', width=1, tags='light'
                )

                # 2. Draw the dotted radius circle
                intensity = light.get('intensity', 1) 
                radius_on_canvas = (intensity / TILE_SIZE) * TILE_DISPLAY_SIZE

                if radius_on_canvas > 0:
                    self.canvas.create_oval(
                        canvas_x - radius_on_canvas, canvas_y - radius_on_canvas,
                        canvas_x + radius_on_canvas, canvas_y + radius_on_canvas,
                        outline=fill_color,   # Use the light's color for the outline
                        dash=(4, 6),          # Create a dotted effect (4px on, 6px off)
                        width=1.5,              # Set the thickness of the dotted line
                        tags='light'
                    )

    def draw_objects(self):
        self.canvas.delete('object')
        for i, obj in enumerate(self.objects):
            pos = obj['pos']
            scale = obj.get('scale', [50, 50, 50])
            canvas_x = (pos[0] / TILE_SIZE) * TILE_DISPLAY_SIZE
            canvas_y = (pos[2] / TILE_SIZE) * TILE_DISPLAY_SIZE
            
            half_w = (scale[0] / TILE_SIZE * TILE_DISPLAY_SIZE) / 2
            half_h = (scale[2] / TILE_SIZE * TILE_DISPLAY_SIZE) / 2
            
            outline_color = 'cyan'
            if i == self.selected_object_index:
                outline_color = 'white'

            self.canvas.create_rectangle(canvas_x - half_w, canvas_y - half_h, canvas_x + half_w, canvas_y + half_h,
                                         outline=outline_color, fill="#6464FF", stipple="gray50", tags=('object', f'object_{i}'))

    def update_light_list(self):
        """Updates the list of lights in the sidebar's light frame."""
        if hasattr(self, 'sidebar') and hasattr(self.sidebar, 'lights_frame'):
            self.sidebar.lights_frame.update_light_list()

    def update_object_list(self):
        pass

    def set_player_pos(self, event):
        self.save_state()
        self.player_start['x'] = (event.x / TILE_DISPLAY_SIZE) * TILE_SIZE
        self.player_start['y'] = (event.y / TILE_DISPLAY_SIZE) * TILE_SIZE
        self.draw_player_start()
        self.check_player_start_validity()

    def set_player_dir(self, event):
        px = (self.player_start['x'] / TILE_SIZE * TILE_DISPLAY_SIZE)
        py = (self.player_start['y'] / TILE_SIZE * TILE_DISPLAY_SIZE)
        self.player_start['angle'] = math.atan2(-(event.y - py), event.x - px)
        self.draw_player_start()

    def draw_player_start(self):
        self.canvas.delete('player_start')
        canvas_x = (self.player_start['x'] / TILE_SIZE) * TILE_DISPLAY_SIZE
        canvas_y = (self.player_start['y'] / TILE_SIZE) * TILE_DISPLAY_SIZE
        angle = self.player_start['angle']
        self.canvas.create_oval(canvas_x - 5, canvas_y - 5, canvas_x + 5, canvas_y + 5, fill='red', tags='player_start')
        self.canvas.create_line(canvas_x, canvas_y, canvas_x + 20 * math.cos(angle), canvas_y - 20 * math.sin(angle), fill='red', width=2, tags='player_start')

    def draw_realtime_player_indicator(self):
        """Draws the player indicator based on the real-time position from the game engine."""
        # FIX: Check if game_view exists and is running before accessing its attributes.
        if not (self.game_view and self.game_view.running):
            return

        # This part remains the same
        player_pos = self.game_view.player_pos
        px, pz = player_pos[0], player_pos[2]
        
        canvas_x = (px / TILE_SIZE) * TILE_DISPLAY_SIZE
        canvas_y = (pz / TILE_SIZE) * TILE_DISPLAY_SIZE
        
        self.canvas.create_oval(
            canvas_x - 5, canvas_y - 5,
            canvas_x + 5, canvas_y + 5,
            fill="cyan", outline="white", width=2, tags="player_realtime"
        )

    def on_closing(self):
        self.close_3d_view()
        self.root.destroy()
        
    def close_3d_view(self):
        if self.game_view:
            self.game_view.stop()
            self.game_view = None
            self.canvas.delete("realtime_player")

    def sync_3d_view(self):
        if self.game_view:
            # Pass wall_textures here as well
            self.game_view.set_level_data(
                self.tile_map, self.lights, self.objects, self.textures, self.wall_textures
            )
            self.game_view.set_show_fps(self.show_game_fps)
            
    def mouse_down(self, event):
        self.deselect_all()
        items = self.canvas.find_overlapping(event.x, event.y, event.x, event.y)
        
        light_items = [item for item in items if 'light' in self.canvas.gettags(item)]
        object_items = [item for item in items if 'object' in self.canvas.gettags(item)]
        
        if light_items:
            light_tag = [tag for tag in self.canvas.gettags(light_items[0]) if tag.startswith('light_')][0]
            light_index = int(light_tag.split('_')[1])
            self.select_light(light_index)
            self.dragging_light = True
        elif object_items:
            object_tag = [tag for tag in self.canvas.gettags(object_items[0]) if tag.startswith('object_')][0]
            object_index = int(object_tag.split('_')[1])
            self.select_object(object_index)
            self.dragging_object = True
        else:
            # This is the corrected part:
            col = int(event.x / TILE_DISPLAY_SIZE)
            row = int(event.y / TILE_DISPLAY_SIZE)
            self.paint_tile(row, col)

    def mouse_drag(self, event):
        if self.dragging_light and self.selected_light_index is not None:
            self.move_selected_light(event.x, event.y)
        elif self.dragging_object and self.selected_object_index is not None:
            self.move_selected_object(event.x, event.y)
        else:
            # Calculate row and col from the event before calling paint_tile
            col = int(event.x / TILE_DISPLAY_SIZE)
            row = int(event.y / TILE_DISPLAY_SIZE)
            self.paint_tile(row, col)

    def mouse_up(self, event):
        if self.dragging_light or self.dragging_object:
            self.save_state()
        self.dragging_light = False
        self.dragging_object = False

    def select_light(self, index):
        """Selects a light and updates the UI."""
        self.deselect_all()
        self.selected_light_index = index
        
        # LightsFrame in the sidebar will handle its own UI updates
        
        self.redraw_canvas()

    def select_object(self, index):
        self.deselect_all()
        self.selected_object_index = index
        self.update_object_properties_ui()
        self.redraw_canvas()

    def deselect_all(self):
        """Deselects any selected light or object."""
        self.selected_light_index = None
        self.selected_object_index = None
        
        # LightsFrame in the sidebar will handle its own UI updates
        
        self.redraw_canvas()

    def move_selected_light(self, canvas_x, canvas_y):
        if self.selected_light_index is not None:
            game_x = (canvas_x / TILE_DISPLAY_SIZE) * TILE_SIZE
            game_z = (canvas_y / TILE_DISPLAY_SIZE) * TILE_SIZE
            self.lights[self.selected_light_index]['pos'][0] = game_x
            self.lights[self.selected_light_index]['pos'][2] = game_z
            self.redraw_canvas()
            self.sync_3d_view()
            # This is the corrected part:
            if hasattr(self, 'sidebar') and hasattr(self.sidebar, 'lights_frame'):
                self.sidebar.lights_frame.update_properties_ui_for_selected()

    def move_selected_object(self, canvas_x, canvas_y):
        if self.selected_object_index is not None:
            game_x = round(canvas_x / TILE_DISPLAY_SIZE) * TILE_SIZE
            game_z = round(canvas_y / TILE_DISPLAY_SIZE) * TILE_SIZE
            self.objects[self.selected_object_index]['pos'][0] = game_x
            self.objects[self.selected_object_index]['pos'][2] = game_z
            self.redraw_canvas()
            self.sync_3d_view()

    def update_object_properties_ui(self):
        pass

    def open_map_launcher(self):
        launcher_path = os.path.join("editor", "map_launcher.py")
        subprocess.Popen([sys.executable, launcher_path])

    def disable_ui_for_overlay(self):
        for i in range(self.menubar.winfo_children().__len__()):
            self.menubar.entryconfig(i, state=tk.DISABLED)

        for child in self.controls_frame.winfo_children():
            if isinstance(child, ttk.Notebook):
                for tab_id in child.tabs():
                    child.tab(tab_id, state=tk.DISABLED)
                for widget in child.winfo_children():
                    for sub_widget in widget.winfo_children():
                        sub_widget.config(state=tk.DISABLED)
            else:
                try:
                    child.config(state=tk.DISABLED)
                except tk.TclError:
                    pass

        self.canvas.unbind("<B1-Motion>")
        self.canvas.unbind("<Button-1>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.unbind("<Button-3>")
        self.context_menu.entryconfig("Add Light", state=tk.DISABLED)
        self.context_menu.entryconfig("Model", state=tk.DISABLED)
        self.canvas.unbind("<Control-Button-1>")
        self.canvas.unbind("<B3-Motion>")

        self.root.unbind("l")
        self.root.unbind("L")
        self.root.unbind("<Control-z>")
        self.root.unbind("<Control-y>")
        
    def enable_ui_after_overlay(self):
        for i in range(self.menubar.winfo_children().__len__()):
            self.menubar.entryconfig(i, state=tk.NORMAL)

        for child in self.controls_frame.winfo_children():
            if isinstance(child, ttk.Notebook):
                for tab_id in child.tabs():
                    child.tab(tab_id, state=tk.NORMAL)
                for widget in child.winfo_children():
                    for sub_widget in widget.winfo_children():
                        sub_widget.config(state=tk.NORMAL)
            else:
                try:
                    child.config(state=tk.NORMAL)
                except tk.TclError:
                    pass
        
        self.check_player_start_validity()

        self.canvas.bind("<B1-Motion>", self._canvas_mouse_drag_func)
        self.canvas.bind("<Button-1>", self._canvas_mouse_down_func)
        self.canvas.bind("<ButtonRelease-1>", self._canvas_mouse_up_func)
        self.canvas.bind("<Button-3>", self._canvas_button3_func)
        self.context_menu.entryconfig("Add Light", state=tk.NORMAL)
        self.context_menu.entryconfig("Model", state=tk.NORMAL)
        self.canvas.bind("<Control-Button-1>", self._canvas_set_player_pos_func)
        self.canvas.bind("<B3-Motion>", self._canvas_set_player_dir_func)

        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-y>", lambda e: self.redo())

    def hide_overlay_and_enable_ui(self):
        self.main_menu_overlay.place_forget()
        self.enable_ui_after_overlay()

if __name__ == "__main__":
    main_window = tk.Tk()
    app = LevelEditor(main_window)
    main_window.mainloop()