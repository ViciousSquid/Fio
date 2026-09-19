                        err += dx
                        y1 += sy
                self.carve_path(points)
        self.add_dead_ends(count=random.randint(3, 6))

# ----------------------------------------------------------------------
# Geometry generation with nodraw optimization
# ----------------------------------------------------------------------
def generate_brushes_from_grid(grid_map, wall_tex, floor_tex, yield_hook=None):
    brushes = []
    min_wx = 0
    max_wx = grid_map.w * CELL_SIZE
    min_wz = 0
    max_wz = grid_map.h * CELL_SIZE
    margin = 512
    ground_width = (max_wx - min_wx) + margin*2
    ground_depth = (max_wz - min_wz) + margin*2
    ground_center_x = (min_wx + max_wx) / 2
    ground_center_z = (min_wz + max_wz) / 2

    ground_tex = {}