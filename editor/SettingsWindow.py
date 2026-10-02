import tkinter as tk
from tkinter import filedialog, messagebox
import os
import configparser
import subprocess
import sys

class SettingsWindow:
    """
    Manages the settings window for the level editor.
    """
    def __init__(self, root, editor_app, initial_font_size):
        """
        Initializes the floating settings window.
        """
        self.editor_app = editor_app
        self.maps_path = ""

        self.window = tk.Toplevel(root)
        self.window.title("Settings")
        self.window.geometry("400x500+0+0")
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        main_frame = tk.Frame(self.window, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Map Launcher Section ---
        launcher_frame = tk.LabelFrame(main_frame, text="Map Launcher", padx=10, pady=10)
        #launcher_frame.pack(fill=tk.X, pady=(0, 10))

        self.map_listbox = tk.Listbox(launcher_frame, height=6)
        #self.map_listbox.pack(fill=tk.X, expand=True)

        launch_button = tk.Button(launcher_frame, text="Launch Selected Map", command=self.launch_map)
        #launch_button.pack(pady=(5,0))

        # --- Display Section ---
        display_frame = tk.LabelFrame(main_frame, text="Display", padx=10, pady=10)
        display_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.show_fps_var = tk.BooleanVar()
        self.show_fps_checkbox = tk.Checkbutton(display_frame, text="Show FPS in game window",
                                                variable=self.show_fps_var, command=self.save_display_settings)
        self.show_fps_checkbox.pack(anchor=tk.W)

        font_frame = tk.Frame(display_frame)
        font_frame.pack(fill=tk.X, pady=(10, 0))
        tk.Label(font_frame, text="Font Size:").pack(side=tk.LEFT)
        self.font_size_scale = tk.Scale(font_frame, from_=8, to=24, orient=tk.HORIZONTAL,
                                        command=self.editor_app.update_font_size)
        self.font_size_scale.set(initial_font_size)
        self.font_size_scale.pack(side=tk.RIGHT, expand=True, fill=tk.X)

        # --- Physics Checkbox ---
        physics_frame = tk.LabelFrame(main_frame, text="Physics (Experimental)", padx=10, pady=10)
        physics_frame.pack(fill=tk.X, pady=(0, 10))
        self.physics_var = tk.BooleanVar(value=True)
        self.physics_checkbox = tk.Checkbutton(physics_frame, text="Enable Physics",
                                               variable=self.physics_var, command=self.toggle_physics)
        self.physics_checkbox.pack(anchor=tk.W)

        # --- Controls Section ---
        controls_frame = tk.LabelFrame(main_frame, text="Controls", padx=10, pady=10)
        controls_frame.pack(fill=tk.X, pady=(10, 0), expand=True)

        self.invert_mouse_var = tk.BooleanVar()
        self.invert_mouse_checkbox = tk.Checkbutton(controls_frame, text="Invert Mouse Look",
                                                    variable=self.invert_mouse_var, command=self.save_controls)
        self.invert_mouse_checkbox.pack(anchor=tk.W)

        self.control_keys = {}
        self.control_buttons = {}
        self.binding_in_progress = None

        for control_name in ["forward", "back", "left", "right"]:
            frame = tk.Frame(controls_frame)
            frame.pack(fill=tk.X, pady=2)

            tk.Label(frame, text=f"{control_name.capitalize()}:").pack(side=tk.LEFT, padx=(0, 5))

            self.control_keys[control_name] = tk.StringVar()
            self.control_buttons[control_name] = tk.Button(frame, textvariable=self.control_keys[control_name],
                                                           width=10, command=lambda c=control_name: self.change_key(c))
            self.control_buttons[control_name].pack(side=tk.RIGHT)

        self.load_settings()
        self.window.withdraw() # Hide window initially

    def toggle_visibility(self):
        """Toggles the visibility of the settings window."""
        if self.window.state() == 'normal':
            self.hide()
        else:
            self.show()

    def set_maps_path(self, path):
        """Receives the path to the maps directory and populates the listbox."""
        self.maps_path = path
        self.map_listbox.delete(0, tk.END)
        try:
            for filename in sorted(os.listdir(path)):
                if filename.endswith(".json"):
                    self.map_listbox.insert(tk.END, filename)
        except FileNotFoundError:
            print(f"Map directory not found: {path}")

    def launch_map(self):
        """Launches the game engine with the selected map."""
        selection = self.map_listbox.curselection()
        if not selection:
            messagebox.showwarning("No Map Selected", "Please select a map to launch.")
            return
        
        map_filename = self.map_listbox.get(selection[0])
        try:
            python_executable = sys.executable
            subprocess.Popen([python_executable, "game_engine.py", map_filename])
        except Exception as e:
            messagebox.showerror("Launch Error", f"Failed to launch game_engine.py:\n{e}")

    def toggle_physics(self):
        self.editor_app.physics_enabled = self.physics_var.get()
        self.editor_app.save_config()
        self.editor_app.reload_config_and_update()

    def change_key(self, control_name):
        self.binding_in_progress = control_name
        button = self.control_buttons[control_name]
        button.config(text="Press a key...")
        self.window.bind("<KeyPress>", self._capture_key)

    def _capture_key(self, event):
        if self.binding_in_progress:
            new_key = event.keysym.lower()
            if new_key in ["escape", "return", "enter"]:
                messagebox.showwarning("Invalid Key", "This key cannot be bound.")
                self.control_buttons[self.binding_in_progress].config(text=self.control_keys[self.binding_in_progress].get())
            else:
                self.control_keys[self.binding_in_progress].set(new_key)
                self.control_buttons[self.binding_in_progress].config(text=new_key)
                self.save_controls()
            self.window.unbind("<KeyPress>")
            self.binding_in_progress = None

    def load_settings(self):
        config = self.editor_app.config
        self.physics_var.set(config.getboolean('Settings', 'physics', fallback=True))
        self.show_fps_var.set(config.getboolean('Display', 'show_fps', fallback=False))
        if 'Controls' in config:
            for control, key_var in self.control_keys.items():
                key_var.set(config.get('Controls', control, fallback=key_var.get()))
            self.invert_mouse_var.set(config.getboolean('Controls', 'invert_mouse', fallback=False))

    def save_display_settings(self):
        config = self.editor_app.config
        if 'Display' not in config:
            config.add_section('Display')
        config.set('Display', 'show_fps', str(self.show_fps_var.get()))
        self.editor_app.save_config()
        self.editor_app.reload_config_and_update()

    def save_controls(self):
        config = self.editor_app.config
        if 'Controls' not in config:
            config.add_section('Controls')
        for control, key_var in self.control_keys.items():
            config.set('Controls', control, key_var.get())
        config.set('Controls', 'invert_mouse', str(self.invert_mouse_var.get()))
        self.editor_app.save_config()
        self.editor_app.reload_config_and_update()

    def show(self):
        """Makes the settings window visible."""
        self.window.deiconify()

    def hide(self):
        """Hides the settings window."""
        self.window.withdraw()