# editor/lights.py

import tkinter as tk
from tkinter import ttk, colorchooser

class LightsFrame(ttk.Frame):
    def __init__(self, master, editor):
        super().__init__(master)
        self.editor = editor

        # Store the currently selected light index
        self.selected_light_index = tk.IntVar(value=-1)
        self.selected_light_index.trace_add("write", self.on_light_selection_change)

        self._setup_ui()
        self.update_light_list()

    def _setup_ui(self):
        # --- Light List ---
        list_frame = ttk.LabelFrame(self, text="Lights")
        list_frame.pack(fill="x", expand=True, pady=5, padx=5)

        self.light_listbox = tk.Listbox(list_frame, exportselection=False, height=6)
        self.light_listbox.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        self.light_listbox.bind("<<ListboxSelect>>", self.on_select_light)

        list_button_frame = ttk.Frame(list_frame)
        list_button_frame.pack(side="right", fill="y", padx=(0, 5))

        ttk.Button(list_button_frame, text="+", command=self.add_light, width=2).pack(pady=2)
        ttk.Button(list_button_frame, text="-", command=self.delete_light, width=2).pack(pady=2)

        # --- Light Properties ---
        self.properties_frame = ttk.LabelFrame(self, text="Properties")
        self.properties_frame.pack(fill="x", expand=True, pady=5, padx=5)

        self.update_light_properties_ui()

    def update_light_properties_ui(self, *_):
        for widget in self.properties_frame.winfo_children():
            widget.destroy()

        if self.selected_light_index.get() == -1 or not self.editor.lights:
            ttk.Label(self.properties_frame, text="No light selected.").pack(pady=20, padx=10)
            return

        light = self.editor.lights[self.selected_light_index.get()]

        # Color
        color_frame = ttk.Frame(self.properties_frame)
        color_frame.pack(fill='x', padx=5, pady=5)
        ttk.Label(color_frame, text="Color:").pack(side="left")
        self.color_swatch = tk.Label(color_frame, text="      ", bg=f"#{int(light['color'][0]*255):02x}{int(light['color'][1]*255):02x}{int(light['color'][2]*255):02x}")
        self.color_swatch.pack(side="left", padx=5)
        ttk.Button(color_frame, text="Change...", command=self.change_color).pack(side="left")

        # Intensity
        intensity_frame = ttk.Frame(self.properties_frame)
        intensity_frame.pack(fill='x', padx=5, pady=5)
        ttk.Label(intensity_frame, text="Intensity:").pack(side="left", anchor='w')
        self.intensity_var = tk.DoubleVar(value=light['intensity'])
        ttk.Scale(intensity_frame, from_=0, to=10, orient="horizontal", variable=self.intensity_var, command=self.update_light_property).pack(fill='x', expand=True)

        # Height
        height_frame = ttk.Frame(self.properties_frame)
        height_frame.pack(fill='x', padx=5, pady=5)
        ttk.Label(height_frame, text="Height:").pack(side="left", anchor='w')
        self.height_var = tk.DoubleVar(value=light['pos'][1])
        ttk.Scale(height_frame, from_=0, to=1000, orient="horizontal", variable=self.height_var, command=self.update_light_property).pack(fill='x', expand=True)

    def on_light_selection_change(self, *_):
        self.update_light_properties_ui()
        self.editor.select_light(self.selected_light_index.get())

    def update_light_list(self):
        self.light_listbox.delete(0, tk.END)
        for i, light in enumerate(self.editor.lights):
            self.light_listbox.insert(tk.END, f"Light {i+1}")
        
        if self.editor.lights:
            if self.selected_light_index.get() >= len(self.editor.lights):
                self.selected_light_index.set(len(self.editor.lights) - 1)
            elif self.selected_light_index.get() == -1:
                 self.selected_light_index.set(0)

            self.light_listbox.selection_clear(0, tk.END)
            self.light_listbox.selection_set(self.selected_light_index.get())
            self.light_listbox.activate(self.selected_light_index.get())
        else:
            self.selected_light_index.set(-1)
        
        self.update_light_properties_ui()

    def on_select_light(self, event):
        selected_indices = self.light_listbox.curselection()
        if selected_indices:
            self.selected_light_index.set(selected_indices[0])

    def add_light(self):
        self.editor.add_light()
        self.update_light_list()

    def delete_light(self):
        if self.selected_light_index.get() != -1:
            self.editor.delete_light(self.selected_light_index.get())
            self.update_light_list()

    def update_light_property(self, *_):
        if self.selected_light_index.get() != -1:
            light = self.editor.lights[self.selected_light_index.get()]
            light['intensity'] = self.intensity_var.get()
            light['pos'][1] = self.height_var.get()
            self.editor.sync_3d_view()

    def change_color(self):
        if self.selected_light_index.get() != -1:
            light = self.editor.lights[self.selected_light_index.get()]
            initial_color = f"#{int(light['color'][0]*255):02x}{int(light['color'][1]*255):02x}{int(light['color'][2]*255):02x}"
            color_code = colorchooser.askcolor(title="Choose color", initialcolor=initial_color)
            if color_code and color_code[1]:
                self.color_swatch.config(bg=color_code[1])
                rgb_float = [c/255.0 for c in color_code[0]]
                light['color'] = rgb_float
                self.editor.sync_3d_view()