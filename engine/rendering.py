# engine/rendering.py
from OpenGL.GL import *
from OpenGL.GLU import *
import pygame
from .constants import TILE_SIZE, WALL_HEIGHT, FLOOR_HEIGHT, WALL_TILE

def draw_world_cubes(tile_map, grid_width, grid_height):
    glColor3f(0.7, 0.7, 0.7); glBegin(GL_QUADS)
    for r in range(grid_height):
        for c in range(grid_width):
            if tile_map[r, c] == WALL_TILE:
                x, z = c * TILE_SIZE, r * TILE_SIZE
                if c + 1 >= grid_width or tile_map[r, c + 1] != WALL_TILE: draw_cube_face('right', x, z)
                if c - 1 < 0 or tile_map[r, c - 1] != WALL_TILE: draw_cube_face('left', x, z)
                if r + 1 >= grid_height or tile_map[r + 1, c] != WALL_TILE: draw_cube_face('front', x, z)
                if r - 1 < 0 or tile_map[r - 1, c] != WALL_TILE: draw_cube_face('back', x, z)
                draw_cube_face('top', x, z); draw_cube_face('bottom', x, z)
    glEnd()
    
    glColor3f(0.5, 0.5, 0.5); glBegin(GL_QUADS)
    glNormal3f(0, 1, 0)
    glVertex3f(0, FLOOR_HEIGHT, 0); glVertex3f(grid_width * TILE_SIZE, FLOOR_HEIGHT, 0)
    glVertex3f(grid_width * TILE_SIZE, FLOOR_HEIGHT, grid_height * TILE_SIZE); glVertex3f(0, FLOOR_HEIGHT, grid_height * TILE_SIZE)
    glEnd()

def draw_objects(objects, loaded_models):
    glColor3f(0.8, 0.3, 0.3)
    for obj_data in objects:
        model = loaded_models.get(obj_data['path'])
        if model and model.is_loaded:
            glPushMatrix()
            try:
                glTranslatef(obj_data['pos'][0], obj_data['pos'][1], obj_data['pos'][2])
                glScalef(*obj_data.get('scale', [1,1,1]))
                model.render()
            except Exception as e:
                print(f"Error rendering model {obj_data['path']}: {e}")
            finally:
                glPopMatrix()

def draw_cube_face(face, x, z):
    s, h, y = TILE_SIZE, WALL_HEIGHT, FLOOR_HEIGHT
    if   face == 'top':    glNormal3f(0, 1, 0); glVertex3f(x, y + h, z); glVertex3f(x + s, y + h, z); glVertex3f(x + s, y + h, z + s); glVertex3f(x, y + h, z + s)
    elif face == 'bottom': glNormal3f(0, -1, 0); glVertex3f(x, y, z); glVertex3f(x, y, z + s); glVertex3f(x + s, y, z + s); glVertex3f(x + s, y, z)
    elif face == 'front':  glNormal3f(0, 0, 1); glVertex3f(x, y, z + s); glVertex3f(x + s, y, z + s); glVertex3f(x + s, y + h, z + s); glVertex3f(x, y + h, z + s)
    elif face == 'back':   glNormal3f(0, 0, -1); glVertex3f(x, y, z); glVertex3f(x, y + h, z); glVertex3f(x + s, y + h, z); glVertex3f(x + s, y, z)
    elif face == 'left':   glNormal3f(-1, 0, 0); glVertex3f(x, y, z); glVertex3f(x, y, z + s); glVertex3f(x, y + h, z + s); glVertex3f(x, y + h, z)
    elif face == 'right':  glNormal3f(1, 0, 0); glVertex3f(x + s, y, z); glVertex3f(x + s, y + h, z); glVertex3f(x + s, y + h, z + s); glVertex3f(x + s, y, z + s)

def draw_light_visuals(lights, phong_enabled, phong_shader):
    glDisable(GL_LIGHTING)
    if phong_enabled: shaders.glUseProgram(0)
    glPointSize(10.0)
    glBegin(GL_POINTS)
    for light in lights:
        glColor3fv(light['color']); glVertex3fv(light['pos'])
    glEnd()
    if phong_enabled: shaders.glUseProgram(phong_shader)
    glEnable(GL_LIGHTING)
    
def draw_fps(display, font, clock):
    """Renders and displays the current FPS."""
    fps_text = f"FPS: {int(clock.get_fps())}"
    text_surface = font.render(fps_text, True, (255, 255, 255)) # White color
    text_data = pygame.image.tostring(text_surface, "RGBA", True)

    # Disable 3D rendering for 2D overlay
    glMatrixMode(GL_PROJECTION)
    glPushMatrix()
    glLoadIdentity()
    gluOrtho2D(0, display[0], display[1], 0) # Set up 2D orthographic projection
    glMatrixMode(GL_MODELVIEW)
    glPushMatrix()
    glLoadIdentity()

    glDisable(GL_DEPTH_TEST)
    glEnable(GL_BLEND)
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

    # Adjusted Y position by adding 40
    glRasterPos2d(display[0] - text_surface.get_width() - 10, 10 + 40) # Top right corner, moved down by 40 pixels
    glDrawPixels(text_surface.get_width(), text_surface.get_height(), GL_RGBA, GL_UNSIGNED_BYTE, text_data)

    glDisable(GL_BLEND)
    glEnable(GL_DEPTH_TEST)

    # Restore 3D rendering
    glMatrixMode(GL_PROJECTION)
    glPopMatrix()
    glMatrixMode(GL_MODELVIEW)
    glPopMatrix()