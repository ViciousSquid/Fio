import pygame
from pygame.locals import *
from OpenGL.GL import *
from OpenGL.GLU import *
import numpy as np
import json
import sys
from PIL import Image

# Constants
TILE_SIZE = 64.0
WALL_HEIGHT = 148
FLOOR_HEIGHT = 0.0

# Tile types
WALL_TILE = 0
FLOOR_TILE = 1
PORTAL_A_TILE = 3
PORTAL_B_TILE = 4

vertex_shader_source = """
#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;

out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoords;

uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;

void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = mat3(transpose(inverse(model))) * aNormal;
    TexCoords = aTexCoords;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}
"""

fragment_shader_source = """
#version 330 core
out vec4 FragColor;

in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoords;

struct Light {
    vec3 pos;
    vec3 color;
    float intensity;
};

uniform sampler2D u_texture_0;
uniform Light lights[16];
uniform int lightCount;
uniform vec3 viewPos;

void main() {
    vec3 norm = normalize(Normal);
    vec3 ambient = vec3(0.1, 0.1, 0.1); 
    
    vec3 lighting = vec3(0.0);
    for (int i = 0; i < lightCount; i++) {
        float dist = distance(lights[i].pos, FragPos);
        if (dist < lights[i].intensity) {
            vec3 lightDir = normalize(lights[i].pos - FragPos);
            float diff = max(dot(norm, lightDir), 0.0);
            vec3 diffuse = diff * lights[i].color;
            
            float attenuation = 1.0 - (dist / lights[i].intensity);
            attenuation = pow(attenuation, 2.0);

            lighting += diffuse * attenuation;
        }
    }
    
    vec4 texColor = texture(u_texture_0, TexCoords);
    if(texColor.a < 0.1)
        discard;

    vec3 result = (ambient + lighting) * texColor.rgb;
    FragColor = vec4(result, 1.0);
}
"""

class GameView:
    def __init__(self, parent, tile_map, player_start, lights, objects, textures, wall_textures, physics_enabled=True, show_fps=False):
        self.parent = parent
        self.tile_map = tile_map
        self.player_start = player_start
        self.lights = lights
        self.objects = objects
        self.raw_textures_data = textures
        self.wall_textures = wall_textures
        self.physics_enabled = physics_enabled
        self.show_fps = show_fps

        self.screen = None
        self.running = False
        self.clock = pygame.time.Clock()
        self.player_pos = np.array([self.player_start['x'], 32.0, self.player_start['y']], dtype=np.float32)
        self.camera_angle = self.player_start['angle']
        
        self.on_close_callback = None
        self.shader_program = None
        self.meshes = {} 
        self.textures = {}

    def run(self):
        pygame.init()
        self.screen = pygame.display.set_mode((1280, 720), DOUBLEBUF | OPENGL)
        pygame.display.set_caption("Game View")

        self._init_gl()
        self.textures = self.load_textures(self.raw_textures_data)
        self._create_buffers()
        self._setup_uniforms()
        
        self.running = True
        while self.running:
            self.handle_events()
            self.update()
            self.render()
            self.clock.tick(60)

        pygame.quit()
        if self.on_close_callback:
            self.on_close_callback()

    def set_level_data(self, tile_map, lights, objects, textures, wall_textures):
        self.tile_map = tile_map
        self.lights = lights
        self.objects = objects
        self.raw_textures_data = textures
        self.wall_textures = wall_textures
        
        self.textures = self.load_textures(self.raw_textures_data)
        self._create_buffers()
        self._setup_uniforms()

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT or (event.type == KEYDOWN and event.key == K_ESCAPE):
                self.running = False

    def update(self):
        keys = pygame.key.get_pressed()
        forward = np.array([np.cos(self.camera_angle), 0, -np.sin(self.camera_angle)])
        right = np.array([np.sin(self.camera_angle), 0, np.cos(self.camera_angle)])
        
        speed = 3.0
        if keys[K_w]: self.player_pos += forward * speed
        if keys[K_s]: self.player_pos -= forward * speed
        if keys[K_a]: self.player_pos -= right * speed
        if keys[K_d]: self.player_pos += right * speed
        
        rel_x, _ = pygame.mouse.get_rel()
        self.camera_angle -= rel_x * 0.005
        pygame.mouse.set_visible(False)
        pygame.event.set_grab(True)

    def render(self):
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glUseProgram(self.shader_program)
        
        view = self._get_view_matrix()
        projection = self._get_projection_matrix()
        
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "view"), 1, GL_FALSE, view)
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "projection"), 1, GL_FALSE, projection)
        
        model = np.identity(4, dtype=np.float32)
        glUniformMatrix4fv(glGetUniformLocation(self.shader_program, "model"), 1, GL_FALSE, model)

        glUniform3fv(glGetUniformLocation(self.shader_program, "viewPos"), 1, self.player_pos)
        self._update_lights_uniform()
        
        for texture_name, mesh_data in self.meshes.items():
            texture = self.textures.get(texture_name)
            if texture:
                glActiveTexture(GL_TEXTURE0)
                glBindTexture(GL_TEXTURE_2D, texture)
                glUniform1i(glGetUniformLocation(self.shader_program, "u_texture_0"), 0)

            glBindVertexArray(mesh_data['vao'])
            glDrawArrays(GL_QUADS, 0, mesh_data['vertex_count'])

        pygame.display.flip()

    def _init_gl(self):
        glClearColor(0.1, 0.1, 0.1, 1)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        self.shader_program = self._compile_shaders()

    def _compile_shaders(self):
        vertex_shader = glCreateShader(GL_VERTEX_SHADER)
        glShaderSource(vertex_shader, vertex_shader_source)
        glCompileShader(vertex_shader)
        if not glGetShaderiv(vertex_shader, GL_COMPILE_STATUS):
            raise Exception(f"Vertex Shader compilation error: {glGetShaderInfoLog(vertex_shader)}")

        fragment_shader = glCreateShader(GL_FRAGMENT_SHADER)
        glShaderSource(fragment_shader, fragment_shader_source)
        glCompileShader(fragment_shader)
        if not glGetShaderiv(fragment_shader, GL_COMPILE_STATUS):
            raise Exception(f"Fragment Shader compilation error: {glGetShaderInfoLog(fragment_shader)}")

        program = glCreateProgram()
        glAttachShader(program, vertex_shader)
        glAttachShader(program, fragment_shader)
        glLinkProgram(program)
        if not glGetProgramiv(program, GL_LINK_STATUS):
            raise Exception(f"Shader linking error: {glGetProgramInfoLog(program)}")
        glDeleteShader(vertex_shader)
        glDeleteShader(fragment_shader)
        return program
    
    def _create_buffers(self):
        batched_vertex_data = {}
        tex_coords = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0] 
        
        height, width = self.tile_map.shape

        for r in range(height):
            for c in range(width):
                tile_type = self.tile_map[r, c]
                x, z = c * TILE_SIZE, r * TILE_SIZE

                # --- FLOOR TILE ---
                if tile_type == FLOOR_TILE:
                    texture_name = 'default'
                    if texture_name not in batched_vertex_data:
                        batched_vertex_data[texture_name] = []
                    
                    vertices = [[x, FLOOR_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, FLOOR_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, FLOOR_HEIGHT, z], [x, FLOOR_HEIGHT, z]]
                    normal = [0, 1, 0]
                    for i, vertex in enumerate(vertices):
                        batched_vertex_data[texture_name].extend(vertex)
                        batched_vertex_data[texture_name].extend(normal)
                        batched_vertex_data[texture_name].extend(tex_coords[i*2:i*2+2])
                
                # --- WALL TILE ---
                elif tile_type == WALL_TILE:
                    tile_textures = self.wall_textures.get(str((r, c)), {})

                    # Define vertices for all possible faces
                    faces = {
                        'S': ([x, FLOOR_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, FLOOR_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, WALL_HEIGHT, z + TILE_SIZE], [x, WALL_HEIGHT, z + TILE_SIZE]),
                        'N': ([x + TILE_SIZE, FLOOR_HEIGHT, z], [x, FLOOR_HEIGHT, z], [x, WALL_HEIGHT, z], [x + TILE_SIZE, WALL_HEIGHT, z]),
                        'E': ([x + TILE_SIZE, FLOOR_HEIGHT, z], [x + TILE_SIZE, FLOOR_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, WALL_HEIGHT, z + TILE_SIZE], [x + TILE_SIZE, WALL_HEIGHT, z]),
                        'W': ([x, FLOOR_HEIGHT, z + TILE_SIZE], [x, FLOOR_HEIGHT, z], [x, WALL_HEIGHT, z], [x, WALL_HEIGHT, z + TILE_SIZE]),
                    }
                    normals = { 'N': [0, 0, -1], 'S': [0, 0, 1], 'E': [1, 0, 0], 'W': [-1, 0, 0] }

                    # Check neighbors to decide which faces to draw
                    # South Face
                    if r == height - 1 or self.tile_map[r + 1, c] != WALL_TILE:
                        texture_name = tile_textures.get('S', 'default')
                        if texture_name not in batched_vertex_data: batched_vertex_data[texture_name] = []
                        for i, vertex in enumerate(faces['S']):
                            batched_vertex_data[texture_name].extend(vertex); batched_vertex_data[texture_name].extend(normals['S']); batched_vertex_data[texture_name].extend(tex_coords[i*2:i*2+2])
                    
                    # North Face
                    if r == 0 or self.tile_map[r - 1, c] != WALL_TILE:
                        texture_name = tile_textures.get('N', 'default')
                        if texture_name not in batched_vertex_data: batched_vertex_data[texture_name] = []
                        for i, vertex in enumerate(faces['N']):
                            batched_vertex_data[texture_name].extend(vertex); batched_vertex_data[texture_name].extend(normals['N']); batched_vertex_data[texture_name].extend(tex_coords[i*2:i*2+2])

                    # East Face
                    if c == width - 1 or self.tile_map[r, c + 1] != WALL_TILE:
                        texture_name = tile_textures.get('E', 'default')
                        if texture_name not in batched_vertex_data: batched_vertex_data[texture_name] = []
                        for i, vertex in enumerate(faces['E']):
                            batched_vertex_data[texture_name].extend(vertex); batched_vertex_data[texture_name].extend(normals['E']); batched_vertex_data[texture_name].extend(tex_coords[i*2:i*2+2])

                    # West Face
                    if c == 0 or self.tile_map[r, c - 1] != WALL_TILE:
                        texture_name = tile_textures.get('W', 'default')
                        if texture_name not in batched_vertex_data: batched_vertex_data[texture_name] = []
                        for i, vertex in enumerate(faces['W']):
                            batched_vertex_data[texture_name].extend(vertex); batched_vertex_data[texture_name].extend(normals['W']); batched_vertex_data[texture_name].extend(tex_coords[i*2:i*2+2])

        # Create VAO/VBO for each batch
        self.meshes = {}
        for texture_name, vertex_data_list in batched_vertex_data.items():
            if not vertex_data_list: continue
            vertex_data = np.array(vertex_data_list, dtype=np.float32)
            vao = glGenVertexArrays(1)
            glBindVertexArray(vao)
            vbo = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, vbo)
            glBufferData(GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, GL_STATIC_DRAW)
            
            # Position
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 8 * sizeof(GLfloat), ctypes.c_void_p(0))
            glEnableVertexAttribArray(0)
            # Normal
            glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 8 * sizeof(GLfloat), ctypes.c_void_p(3 * sizeof(GLfloat)))
            glEnableVertexAttribArray(1)
            # TexCoords
            glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, 8 * sizeof(GLfloat), ctypes.c_void_p(6 * sizeof(GLfloat)))
            glEnableVertexAttribArray(2)

            self.meshes[texture_name] = { 'vao': vao, 'vbo': vbo, 'vertex_count': len(vertex_data) // 8 }

    def _setup_uniforms(self):
        glUseProgram(self.shader_program)
        self._update_lights_uniform()

    def _update_lights_uniform(self):
        light_count = min(len(self.lights), 16)
        glUniform1i(glGetUniformLocation(self.shader_program, "lightCount"), light_count)
        for i, light in enumerate(self.lights[:light_count]):
            # Use .get() to provide a default value if 'intensity' is missing
            intensity = light.get('intensity', 500.0) # Default to 500 if not found

            glUniform3fv(glGetUniformLocation(self.shader_program, f"lights[{i}].pos"), 1, light['pos'])
            glUniform3fv(glGetUniformLocation(self.shader_program, f"lights[{i}].color"), 1, light['color'])
            glUniform1f(glGetUniformLocation(self.shader_program, f"lights[{i}].intensity"), intensity)

    def _get_view_matrix(self):
        target = self.player_pos + np.array([np.cos(self.camera_angle), 0, -np.sin(self.camera_angle)])
        return self._look_at(self.player_pos, target, np.array([0, 1, 0]))

    def _get_projection_matrix(self):
        return self._perspective(45.0, 1280/720, 0.1, 5000.0)
    
    def _look_at(self, position, target, up):
        zaxis = (position - target) / np.linalg.norm(position - target)
        xaxis = np.cross(up, zaxis) / np.linalg.norm(np.cross(up, zaxis))
        yaxis = np.cross(zaxis, xaxis)
        
        translation = np.identity(4)
        translation[3, 0] = -position[0]
        translation[3, 1] = -position[1]
        translation[3, 2] = -position[2]
        
        rotation = np.identity(4)
        rotation[0, 0:3] = xaxis
        rotation[1, 0:3] = yaxis
        rotation[2, 0:3] = zaxis
        
        return np.dot(translation, rotation)

    def _perspective(self, fovy, aspect, zNear, zFar):
        f = 1.0 / np.tan(np.radians(fovy) / 2.0)
        return np.array([
            [f / aspect, 0, 0, 0],
            [0, f, 0, 0],
            [0, 0, (zFar + zNear) / (zNear - zFar), -1],
            [0, 0, (2 * zFar * zNear) / (zNear - zFar), 0]
        ], dtype=np.float32)

    def load_textures(self, texture_dict):
        loaded = {}
        default_img = Image.new('RGB', (64, 64), color = 'darkgray')
        loaded['default'] = self._create_texture(default_img)
        
        for name, path in texture_dict.items():
            if name == 'default' or path is None: continue
            try:
                img = Image.open(path).convert("RGBA")
                loaded[name] = self._create_texture(img)
            except Exception as e:
                print(f"Could not load texture {name} from {path}: {e}")
        return loaded

    def _create_texture(self, image):
        img_data = image.tobytes()
        tex_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        
        pixel_format = GL_RGBA if image.mode == "RGBA" else GL_RGB
        glTexImage2D(GL_TEXTURE_2D, 0, pixel_format, image.width, image.height, 0, pixel_format, GL_UNSIGNED_BYTE, img_data)
        
        return tex_id

    def stop(self):
        self.running = False
        
    def on_close(self, callback):
        self.on_close_callback = callback


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python game_engine.py <path_to_level.json>")
        sys.exit(1)

    level_file_path = sys.argv[1]
    with open(level_file_path, 'r') as f:
        level_data = json.load(f)

    wall_textures_str_keys = level_data.get('wall_textures', {})
    wall_textures_tuple_keys = {eval(k): v for k, v in wall_textures_str_keys.items()}
    
    show_fps_flag = "--show-fps" in sys.argv
    
    game_view = GameView(
        None,
        np.array(level_data['tile_map']),
        level_data['player_start'],
        level_data.get('lights', []),
        level_data.get('objects', []),
        level_data.get('textures', {}),
        wall_textures_tuple_keys,
        show_fps=show_fps_flag
    )
    game_view.run()