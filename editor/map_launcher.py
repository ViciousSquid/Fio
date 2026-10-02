# editor/map_launcher.py

import tkinter as tk
from tkinter import ttk
import os
import sys
import subprocess

class MapLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("Map Launcher")
        self.root.geometry("400x500")
        
        main_frame = tk.Frame(root, padx=10, pady=10)
        main_frame.pack(expand=True, fill=tk.BOTH)
        
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(0, 10))
        
        refresh_button = ttk.Button(button_frame, text="Refresh", command=self.populate_map_list)
        refresh_button.pack()
        
        self.map_list_frame = ttk.Frame(main_frame)
        self.map_list_frame.pack(expand=True, fill=tk.BOTH)
        
        self.populate_map_list()

    def populate_map_list(self):
        for widget in self.map_list_frame.winfo_children():
            widget.destroy()

        maps_dir = "maps"
        if not os.path.exists(maps_dir):
            ttk.Label(self.map_list_frame, text="No maps found. Save a map in the editor first.").pack(pady=20)
            return

        map_files = [f for f in os.listdir(maps_dir) if f.endswith('.json')]

        if not map_files:
            ttk.Label(self.map_list_frame, text="No maps found in the 'maps' directory.").pack(pady=20)
            return

        for map_file in map_files:
            map_path = os.path.join(maps_dir, map_file)
            
            map_frame = ttk.Frame(self.map_list_frame, padding=5, relief=tk.RIDGE, borderwidth=1)
            map_frame.pack(fill=tk.X, pady=5)
            
            ttk.Label(map_frame, text=map_file).pack(side=tk.LEFT, expand=True, fill=tk.X)
            
            launch_button = ttk.Button(map_frame, text="Launch", command=lambda p=map_path: self.launch_map(p))
            launch_button.pack(side=tk.RIGHT)

    def launch_map(self, map_path):
        python_executable = sys.executable
        game_engine_path = "game_engine.py"
        subprocess.Popen([python_executable, game_engine_path, map_path])

if __name__ == "__main__":
    root = tk.Tk()
    app = MapLauncher(root)
    root.mainloop()