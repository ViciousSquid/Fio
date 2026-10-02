# editor/generation_dialog.py

import tkinter as tk
from tkinter import ttk
from generate_level import generate

class GenerationDialog(tk.Toplevel):
    def __init__(self, parent, editor):
        super().__init__(parent)
        self.editor = editor
        self.title("Generate Random Level")
        self.transient(parent)
        self.grab_set()

        self.width_var = tk.IntVar(value=self.editor.grid_width)
        self.height_var = tk.IntVar(value=self.editor.grid_height)
        self.generator_var = tk.StringVar(value="genA")

        self._setup_widgets()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _setup_widgets(self):
        main_frame = ttk.Frame(self, padding="10")
        main_frame.pack(fill="both", expand=True)

        # Size Inputs
        size_frame = ttk.LabelFrame(main_frame, text="Dimensions")
        size_frame.pack(fill="x", pady=5)
        ttk.Label(size_frame, text="Width:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(size_frame, textvariable=self.width_var, width=5).grid(row=0, column=1, padx=5, pady=5)
        ttk.Label(size_frame, text="Height:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(size_frame, textvariable=self.height_var, width=5).grid(row=1, column=1, padx=5, pady=5)

        # Generator Choice
        gen_frame = ttk.LabelFrame(main_frame, text="Generator Type")
        gen_frame.pack(fill="x", pady=5)
        ttk.Radiobutton(gen_frame, text="Generator A", variable=self.generator_var, value="genA").pack(anchor="w", padx=5)
        ttk.Radiobutton(gen_frame, text="Generator B", variable=self.generator_var, value="genB").pack(anchor="w", padx=5)
        
        # Action Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(button_frame, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(button_frame, text="Generate", command=self.generate_level, style="Accent.TButton").pack(side="right", padx=5)

    def generate_level(self):
        width = self.width_var.get()
        height = self.height_var.get()
        generator_type = self.generator_var.get()
        
        new_map = generate(method=generator_type, width=width, height=height)
        
        self.editor.tile_map = new_map
        self.editor.grid_width = width
        self.editor.grid_height = height
        self.editor.redraw_canvas()
        
        self.destroy()