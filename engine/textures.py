from OpenGL.GL import *
from PyQt5.QtGui import QImage

class TextureManager:
    def __init__(self):
        self.textures = {}

    def get(self, path):
        if path not in self.textures:
            self.textures[path] = self._load_texture(path)
        return self.textures[path]

    def _load_texture(self, path):
        image = QImage(path)
        if image.isNull():
            print(f"Error loading image: {path}")
            return -1

        image = image.convertToFormat(QImage.Format_RGBA8888)
        width, height = image.width(), image.height()
        data = image.bits().asstring(image.byteCount())

        texture_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, texture_id)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_REPEAT)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)

        return texture_id

# Instantiate a global texture manager
texture_manager = TextureManager()