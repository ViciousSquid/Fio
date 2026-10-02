import tkinter as tk
from tkinter import ttk

from editor.AssetBrowser import AssetBrowser
from editor.lights import LightsFrame
from editor.generation_dialog import GenerationDialog

# Constants for brush types, mirroring the editor
WALL_TILE = 0
FLOOR_TILE = 1
PORTAL_A_TILE = 3 
PORTAL_B_TILE = 4 

class CollapsibleFrame(tk.Frame):
    """A collapsible frame widget that can be toggled by a button."""
    def __init__(self, parent, text="", is_expanded=False, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.text = text
        self.columnconfigure(0, weight=1)
        style = ttk.Style()
        style.configure("Toggle.TButton", font=("TkDefaultFont", 10))
        self.toggle_button = ttk.Button(self, text=f"▶ {self.text}", command=self.toggle, style="Toggle.TButton")
        self.toggle_button.grid(row=0, column=0, sticky="ew")
        self.sub_frame = tk.Frame(self, relief="sunken", borderwidth=1)
        if is_expanded:
            self.toggle()

    def toggle(self):
        """Toggles the visibility of the sub_frame."""
        if self.sub_frame.winfo_viewable():
            self.sub_frame.grid_forget()
            self.toggle_button.configure(text=f"▶ {self.text}")
        else:
            self.sub_frame.grid(row=1, column=0, sticky="nsew")
            self.toggle_button.configure(text=f"▼ {self.text}")

class Sidebar:
    def __init__(self, parent, editor, brush_var):
        self.editor = editor
        self.brush_var = brush_var  # Store the brush variable
        self.frame = tk.Frame(parent, width=250)
        self.frame.pack(side=tk.RIGHT, padx=10, pady=10, fill=tk.Y)
        self.frame.pack_propagate(False)
        self._setup_widgets()

    def _setup_widgets(self):
        # --- Top Buttons ---
        self._setup_action_buttons()

        # --- Brush Tool ---
        self._setup_brush_tool()
        
        # --- Asset Browser (Collapsible) ---
        asset_frame = CollapsibleFrame(self.frame, text="Asset Browser", is_expanded=True)
        asset_frame.pack(fill="x", pady=2)
        self.editor.asset_browser = AssetBrowser(asset_frame.sub_frame, self.editor)

        # --- Lights Manager (Collapsible) ---
        lights_frame = CollapsibleFrame(self.frame, text="Lights")
        lights_frame.pack(fill="x", pady=2)
        self.editor.lights_frame = LightsFrame(lights_frame.sub_frame, self.editor)
        self.editor.lights_frame.pack(fill="x", expand=True)
        
        # --- Object Manager (Collapsible) ---
        objects_frame = CollapsibleFrame(self.frame, text="Objects")
        objects_frame.pack(fill="x", pady=2)
        add_object_button = ttk.Button(objects_frame.sub_frame, text="Add Object", command=self.editor.add_object)
        add_object_button.pack(pady=5, padx=5, fill="x")

        # --- History Section (Moved to Bottom) ---
        history_frame = CollapsibleFrame(self.frame, text="History")
        history_frame.pack(fill="x", pady=2, side=tk.BOTTOM)
        
        history_list_container = tk.Frame(history_frame.sub_frame)
        history_list_container.pack(fill="both", expand=True, padx=2, pady=2)
        
        self.history_listbox = tk.Listbox(history_list_container, height=8)
        self.history_listbox.pack(side="left", fill="both", expand=True)
        self.history_listbox.bind("<<ListboxSelect>>", self.editor.on_history_select)

        history_buttons_frame = tk.Frame(history_list_container)
        history_buttons_frame.pack(side="right", fill="y", padx=(2,0))
        
        undo_btn = ttk.Button(history_buttons_frame, text="<", command=self.editor.undo, width=2)
        undo_btn.pack(pady=2, anchor="n")

        redo_btn = ttk.Button(history_buttons_frame, text=">", command=self.editor.redo, width=2)
        redo_btn.pack(pady=2, anchor="n")

    def _setup_action_buttons(self):
        launch_map_button = ttk.Button(self.frame, text="Launch map", command=self.editor.open_map_launcher)
        launch_map_button.pack(pady=5, padx=5, fill="x")
        
        load_save_frame = ttk.Frame(self.frame)
        load_save_frame.pack(fill="x", padx=5)
        load_button = ttk.Button(load_save_frame, text="Load", command=self.editor.load_level)
        load_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 2))
        save_button = ttk.Button(load_save_frame, text="Save", command=self.editor.save_level)
        save_button.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(2, 0))

        style = ttk.Style()
        style.configure("Orange.TButton", foreground="black", background="orange")
        generate_button = ttk.Button(self.frame, text="Generate random level", command=self.open_generation_dialog, style="Orange.TButton")
        generate_button.pack(pady=5, padx=5, fill="x")
        
        play_frame = ttk.Frame(self.frame, padding=(5, 5))
        play_frame.pack(fill="x")
        self.play_button = ttk.Button(play_frame, text="▶ Play", command=self.editor.start_game_view)
        self.play_button.pack(fill="x", expand=True)
        self.play_button.pack_forget()

    def _setup_brush_tool(self):
        # Create a frame with a blue tint
        brush_frame = tk.LabelFrame(self.frame, text="Brush", relief="ridge", borderwidth=2, bg="#8cdd93", padx=5, pady=5)
        brush_frame.pack(fill="x", padx=5, pady=(5, 10))

        ttk.Radiobutton(brush_frame, text="Wall", variable=self.brush_var, value=WALL_TILE, command=self.editor.select_brush).pack(anchor="w")
        ttk.Radiobutton(brush_frame, text="Floor", variable=self.brush_var, value=FLOOR_TILE, command=self.editor.select_brush).pack(anchor="w")
        ttk.Radiobutton(brush_frame, text="Portal A", variable=self.brush_var, value=PORTAL_A_TILE, command=self.editor.select_brush).pack(anchor="w")
        ttk.Radiobutton(brush_frame, text="Portal B", variable=self.brush_var, value=PORTAL_B_TILE, command=self.editor.select_brush).pack(anchor="w")

    def open_generation_dialog(self):
        GenerationDialog(self.frame.winfo_toplevel(), self.editor)

    def update_history_list(self, history_items):
        """Refreshes the history listbox with new items."""
        current_selection = self.editor.current_state_index
        self.history_listbox.delete(0, tk.END)
        for item in history_items:
            self.history_listbox.insert(tk.END, item)
        if 0 <= current_selection < len(history_items):
            self.history_listbox.selection_set(current_selection)
            self.history_listbox.activate(current_selection)
            self.history_listbox.see(current_selection)