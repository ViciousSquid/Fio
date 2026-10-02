# In noise_generator.py

import random
import math
import numpy as np
import os

# --- Self-Contained Perlin Noise Implementation ---
# (Adapted from a public domain implementation)
class PerlinNoise:
    def __init__(self, seed=None):
        if seed is None:
            seed = random.randint(0, 2**32 - 1)
        self.seed = seed
        self.p = bytearray(range(256))
        random.seed(seed)
        random.shuffle(self.p)
        self.p.extend(self.p)

    def _fade(self, t):
        return t * t * t * (t * (t * 6 - 15) + 10)

    def _lerp(self, t, a, b):
        return a + t * (b - a)

    def _grad(self, hash_val, x, y, z):
        h = hash_val & 15
        u = x if h < 8 else y
        v = y if h < 4 else (x if h in [12, 14] else z)
        return (u if (h & 1) == 0 else -u) + (v if (h & 2) == 0 else -v)

    def noise(self, x, y, z, octaves=1, persistence=0.5):
        total = 0
        frequency = 1
        amplitude = 1
        max_value = 0
        for _ in range(octaves):
            total += self._noise_single(x * frequency, y * frequency, z * frequency) * amplitude
            max_value += amplitude
            amplitude *= persistence
            frequency *= 2
        return total / max_value

    def _noise_single(self, x, y, z):
        xi = int(x) & 255
        yi = int(y) & 255
        zi = int(z) & 255
        xf, yf, zf = x - int(x), y - int(y), z - int(z)
        u, v, w = self._fade(xf), self._fade(yf), self._fade(zf)
        
        p = self.p
        aaa = p[p[p[xi] + yi] + zi]
        aab = p[p[p[xi] + yi] + zi + 1]
        aba = p[p[p[xi] + yi + 1] + zi]
        abb = p[p[p[xi] + yi + 1] + zi + 1]
        baa = p[p[p[xi + 1] + yi] + zi]
        bab = p[p[p[xi + 1] + yi] + zi + 1]
        bba = p[p[p[xi + 1] + yi + 1] + zi]
        bbb = p[p[p[xi + 1] + yi + 1] + zi + 1]

        x1 = self._lerp(u, self._grad(aaa, xf, yf, zf), self._grad(baa, xf - 1, yf, zf))
        x2 = self._lerp(u, self._grad(aba, xf, yf - 1, zf), self._grad(bba, xf - 1, yf - 1, zf))
        y1 = self._lerp(v, x1, x2)
        x3 = self._lerp(u, self._grad(aab, xf, yf, zf - 1), self._grad(bab, xf - 1, yf, zf - 1))
        x4 = self._lerp(u, self._grad(abb, xf, yf - 1, zf - 1), self._grad(bbb, xf - 1, yf - 1, zf - 1))
        y2 = self._lerp(v, x3, x4)

        return self._lerp(w, y1, y2)

# --- Texture Generation Function ---
def generate_3d_noise_texture(size=32, frequency=4.0, filename="assets/noise_3d.bin"):
    if not os.path.exists('assets'):
        os.makedirs('assets')

    print(f"Generating {size}x{size}x{size} 3D noise texture...")
    
    noise_gen = PerlinNoise()
    noise_data = np.zeros((size, size, size), dtype=np.uint8)

    for z in range(size):
        for y in range(size):
            for x in range(size):
                value = noise_gen.noise(x / size * frequency, y / size * frequency, z / size * frequency, octaves=4)
                scaled_value = int((value + 1.0) * 0.5 * 255)
                noise_data[z, y, x] = np.uint8(scaled_value)
    
    with open(filename, 'wb') as f:
        f.write(noise_data.tobytes())
        
    print(f"✅ Successfully saved noise data to '{filename}'")

if __name__ == '__main__':
    generate_3d_noise_texture()