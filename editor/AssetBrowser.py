import tkinter as tk
from tkinter import ttk, filedialog
from PIL import Image, ImageTk
import os

class AssetBrowser:
    def __init__(self, parent, editor):
        self.parent = parent
        self.editor = editor
        self.textures = {}  # {name: {path, image, photo_image}}

        self.main_frame = ttk.Frame(parent)
        self.main_frame.pack(fill="both", expand=True)

        self.texture_list_frame = ttk.Frame(self.main_frame)
        self.texture_list_frame.pack(fill="x")

        self.add_texture_button = ttk.Button(self.main_frame, text="Add Texture", command=self.add_texture)
        self.add_texture_button.pack(fill="x", pady=5, padx=5)

    def add_texture(self):
        filepath = filedialog.askopenfilename(
            title="Select Texture",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.bmp *.gif"), ("All files", "*.*")]
        )
        if not filepath:
            return

        basename = os.path.basename(filepath)
        name, _ = os.path.splitext(basename)
        
        if name in self.textures:
            print(f"Texture with name '{name}' already exists.")
            return

        self.load_texture(filepath, name)
        self.repopulate_display()

    def load_texture(self, filepath, name):
        try:
            img = Image.open(filepath).resize((48, 48))
            self.textures[name] = {
                "path": filepath,
                "image": img,
                "photo_image": ImageTk.PhotoImage(img)
            }
            self.editor.textures[name] = filepath
        except Exception as e:
            print(f"Failed to load texture {name}: {e}")

    def repopulate_display(self):
        for widget in self.texture_list_frame.winfo_children():
            widget.destroy()

        for name, data in self.textures.items():
            btn = ttk.Button(self.texture_list_frame, image=data["photo_image"], text=name, compound="top",
                             command=lambda n=name, p=data["path"]: self.select_texture(n, p))
            btn.pack(side="left", padx=2, pady=2)
    
    def repopulate_from_data(self, texture_data):
        self.textures.clear()
        for name, path in texture_data.items():
            if name != 'default' and path is not None:
                self.load_texture(path, name)
        self.repopulate_display()

    def select_texture(self, name, path):
        self.editor.set_active_texture(path, name)
        self.editor.brush_var.set(self.editor.TEXTURE_BRUSH)
        self.editor.select_brush()