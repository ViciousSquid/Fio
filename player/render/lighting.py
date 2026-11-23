"""Compact std140 storage preserves all desktop light values on GLES drivers."""
import numpy as np
from engine.shaders import MAX_LIGHTS_PLAYER as MAX_LIGHTS, LIGHT_UBO_BINDING, COOKIE_TEXTURE_UNIT


LIGHT_DTYPE = np.dtype([('position_radius','<f4',4), ('color_intensity','<f4',4),
                       ('indices','<i4',4)])


def batch_lights(lights, center, half, limit):
    """Rank lights that can reach the batch, including lights behind the eye."""
    if not lights:
        return ()
    positions = np.asarray([light['pos'] for light in lights])
    radius = np.asarray([max(.001, light['radius']) for light in lights])
    delta = np.maximum(np.abs(positions-center)-half, 0)
    distance = np.sqrt(np.sum(delta*delta, axis=1))
    strength = np.asarray([light['intensity']*max(light['color']) for light in lights])
    score = np.maximum(0, 1-distance/radius)**2 * strength
    candidates = np.flatnonzero(score > 0)
    chosen = candidates[np.argsort(-score[candidates], kind='stable')[:limit]]
    # Stable ordering minimises uniform changes when ranking alone changes.
    return tuple(sorted(map(int, chosen)))


class LightBuffer:
    def __init__(self, gl, program):
        self.gl = gl
        self.data = np.zeros(MAX_LIGHTS, dtype=LIGHT_DTYPE)
        self.buffer = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self.buffer)
        gl.glBufferData(gl.GL_UNIFORM_BUFFER, self.data.nbytes, None, gl.GL_DYNAMIC_DRAW)
        block = gl.glGetUniformBlockIndex(program, 'FioLightBlock')
        gl.glUniformBlockBinding(program, block, LIGHT_UBO_BINDING)
        self.program = program
        #: The played package's image reader (path -> bytes or None), set by
        #: the renderer per scene; the shared cookie array is made on demand.
        self.read = None
        self.cookies = None
        self._cookie_unit_set = False

    def forget_cookies(self):
        """A new scene: its images may differ under the same names."""
        if self.cookies is not None:
            self.cookies.close()
            self.cookies = None

    def _cookie_layers(self, lights):
        """Each light's cookie layer + 1 (0: none), the desktop's indices.z.
        None, and nothing loaded or bound, when no light has a cookie."""
        ids = [int(light.get('cookie', 0) or 0) for light in lights]
        if not any(ids):
            self._bind_cookie_placeholder()
            return None
        if self.cookies is None:
            from engine.light_cookies import COOKIE_SIZE_LOW_POWER, CookieArray
            self.cookies = CookieArray(self.gl, read=self.read or (lambda _path: None),
                                       size=COOKIE_SIZE_LOW_POWER)
        layers = self.cookies.layers(ids)
        if not self.cookies.bind(COOKIE_TEXTURE_UNIT):
            self._bind_cookie_placeholder()
        return layers

    def _bind_cookie_placeholder(self):
        """A complete 1x1 array on the cookie unit while no cookie array is
        bound: a driver may check every active sampler at draw time."""
        from engine.light_cookies import bind_placeholder
        cache = self.__dict__.setdefault('_cookie_gl_cache', {})
        bind_placeholder(self.gl, COOKIE_TEXTURE_UNIT, cache)

    def upload(self, lights, shadows):
        """Called with the world program current."""
        gl = self.gl
        if not self._cookie_unit_set:
            # Off unit 0, where the sampler2D diffuse lives.
            loc = gl.glGetUniformLocation(self.program, 'fioCookies')
            if loc != -1:
                gl.glUniform1i(loc, COOKIE_TEXTURE_UNIT)
            self._cookie_unit_set = True
        self.data.fill(0)
        cookies = self._cookie_layers(lights[:MAX_LIGHTS])
        if cookies is not None:
            self.data['indices'][:len(cookies), 2] = cookies
            # The cookie's shadow floor (desktop params.w), in thousandths.
            self.data['indices'][:len(cookies), 3] = [
                int(round(1000.0 * float(light.get('cookie_floor', 0.0))))
                for light in lights[:len(cookies)]]
        for row, light in enumerate(lights[:MAX_LIGHTS]):
            self.data['position_radius'][row,:3] = light['pos']
            self.data['position_radius'][row,3] = light['radius']
            self.data['color_intensity'][row,:3] = light['color']
            self.data['color_intensity'][row,3] = light['intensity']
            self.data['indices'][row,0] = shadows.get(row,-1)
            # The source's size (an emissive brush; 0 for a point light).
            self.data['indices'][row,1] = int(round(float(light.get('size', 0.0))))
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self.buffer)
        gl.glBufferSubData(gl.GL_UNIFORM_BUFFER, 0, self.data.nbytes, self.data)
        gl.glBindBufferBase(gl.GL_UNIFORM_BUFFER, LIGHT_UBO_BINDING, self.buffer)

    def close(self):
        self.forget_cookies()
        cache = self.__dict__.get('_cookie_gl_cache')
        if cache:
            from engine.light_cookies import release_placeholder
            release_placeholder(self.gl, cache)
        self.gl.glDeleteBuffers(1, [self.buffer])
