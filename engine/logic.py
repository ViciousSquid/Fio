# engine/logic.py
import pygame
from pygame.locals import *
import math
from OpenGL.GL import *
from OpenGL.GLU import *
from OpenGL.GL import shaders
from . import rendering

def update(game_view):
    try:
        game_view.clock.tick() # New: Update the clock for FPS calculation

        for event in pygame.event.get():
            if event.type == pygame.QUIT: game_view.running = False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    if game_view.edit_mode: pygame.event.set_grab(not pygame.event.get_grab()); pygame.mouse.set_visible(not pygame.mouse.get_visible())
                    else: game_view.running = False
                if event.key == pygame.K_p: game_view.toggle_phong_shading()
                if event.key == pygame.K_l: game_view.show_light_visuals = not game_view.show_light_visuals
            if event.type == VIDEORESIZE: game_view.display = event.size; glViewport(0, 0, *game_view.display)

        if not game_view.running: return
        if pygame.event.get_grab() or game_view.edit_mode: game_view.player.update(game_view.tile_map)

        glClearColor(0.1, 0.1, 0.15, 1.0); glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT); glLoadIdentity()
        gluPerspective(70, (game_view.display[0] / game_view.display[1]) if game_view.display[1] > 0 else 1, 0.1, game_view.MAX_DEPTH)
        
        glRotatef(-math.degrees(game_view.player.angle), 0, 1, 0)
        glTranslatef(-game_view.player.x, -game_view.player.height - game_view.player.z, -game_view.player.y)
        
        if game_view.phong_enabled and game_view.phong_shader:
            shaders.glUseProgram(game_view.phong_shader)
            glUniform3f(game_view.u_view_pos, game_view.player.x, game_view.player.height + game_view.player.z, game_view.player.y)
            num_lights = min(len(game_view.lights), 10)
            glUniform1i(game_view.u_num_lights, num_lights)
            for i in range(num_lights):
                glUniform3fv(game_view.u_light_pos_locations[i], 1, game_view.lights[i]["pos"])
                glUniform3fv(game_view.u_light_color_locations[i], 1, game_view.lights[i]["color"])
                glUniform1f(game_view.u_light_intensity_locations[i], game_view.lights[i].get("intensity", 1.0))

        rendering.draw_world_cubes(game_view.tile_map, game_view.grid_width, game_view.grid_height)
        rendering.draw_objects(game_view.objects_data, game_view.loaded_models)

        if game_view.show_light_visuals: rendering.draw_light_visuals(game_view.lights, game_view.phong_enabled, game_view.phong_shader)

        if game_view.phong_enabled and game_view.phong_shader: shaders.glUseProgram(0)

        # New: Draw FPS
        if game_view.show_fps and game_view.font:
            rendering.draw_fps(game_view.display, game_view.font, game_view.clock)

        pygame.display.flip()
    except Exception as e:
        print(f"An error occurred in the game loop: {e}")
        game_view.running = False