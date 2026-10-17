"""Play the engine's audio requests directly from the universal archive."""
import math


class PlayerAudio:
    def __init__(self, package, mixer=None):
        if mixer is None:
            import pygame
            mixer = pygame.mixer
        self.mixer, self.package = mixer, package
        self.cache, self.channels = {}, {}
        self.available = True
        try:
            if not mixer.get_init():
                mixer.init()
            mixer.set_num_channels(32)
        except Exception as exc:
            self.available = False
            print(f'[Audio] Output unavailable: {exc}')
        self._paused = False

    def process(self, requests, eye, yaw):
        if not self.available:
            return
        for request in requests:
            key = request.get('entity_id')
            if request.get('action') == 'stop':
                if key in self.channels:
                    self.channels.pop(key)[0].stop()
                continue
            name = request.get('file')
            if not name:
                continue
            if name not in self.cache:
                stream = self.package.open_asset(name)
                if stream is None:
                    print(f'[Audio] Missing package sound: {name}')
                    continue
                self.cache[name] = self.mixer.Sound(file=stream)
            channel = self.mixer.find_channel()
            if channel is None:
                continue
            if key in self.channels:
                self.channels.pop(key)[0].stop()
            channel.play(self.cache[name], loops=-1 if request.get('looping') else 0)
            self.channels[key if key is not None else id(channel)] = (channel, dict(request))
        for key, (channel, request) in list(self.channels.items()):
            if not channel.get_busy():
                del self.channels[key]
                continue
            volume = max(0., min(1., float(request.get('volume', 1))))
            position = request.get('position')
            pan = 0.
            if position is not None and not request.get('global', False):
                delta = [position[i] - eye[i] for i in range(3)]
                distance = math.sqrt(sum(v * v for v in delta))
                volume *= max(0., 1 - distance / max(.001, float(request.get('radius', 512))))
                right = (-math.sin(math.radians(yaw)), math.cos(math.radians(yaw)))
                pan = max(-1., min(1., (delta[0] * right[0] + delta[2] * right[1]) / max(distance, .001)))
            channel.set_volume(volume * min(1., 1 - pan), volume * min(1., 1 + pan))

    def pause(self, paused):
        if self.available and self._paused != paused:
            self._paused = paused
            self.mixer.pause() if paused else self.mixer.unpause()

    def close(self):
        for channel, _ in self.channels.values():
            channel.stop()
        self.channels.clear()
        self.cache.clear()
