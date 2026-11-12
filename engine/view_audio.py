"""The 3D view's audio, delegated from QtGameView.

Owns the pygame mixer, the cache of loaded sounds (loose files and the
``sounds``/``music`` archives), the queue of sounds the logic thread asks
for, and the positional mix of speakers against the camera. QtGameView keeps
the frame loop and calls :meth:`_process_sound_queue` once a frame.
"""

import io
import math
import os
import time
import zipfile

import glm
import numpy as np
import pygame


class ViewAudio:
    #: Seconds between attempts to open the audio device after one failed.
    MIXER_RETRY_SECONDS = 10.0

    def __init__(self, view):
        self.view = view
        self.sound_pool = {}
        #: Volumes, 0..1: music (files from assets/music) and every other
        #: game sound (Settings > Play Modes > Audio, and the pause menu).
        self.sound_volume = 1.0
        self.music_volume = 1.0

    def _init_sound_system(self):
        """Preload sounds from assets/sounds.fiopak, or the legacy loose folder."""
        if not self._ensure_pygame_mixer():
            print("[Audio] Sound system unavailable — mixer could not be initialized")
            return

        from engine.audio_archives import AudioArchiveReader

        sound_archive = os.path.join(os.getcwd(), "assets", "sounds.fiopak")
        if os.path.isfile(sound_archive):
            try:
                with AudioArchiveReader(sound_archive, "assets/sounds") as reader:
                    members = reader.members()
                    print("[Audio] Preloading sounds from sounds.fiopak...")
                    count = 0
                    for member in members:
                        try:
                            data = reader.read(member)
                        except (OSError, zipfile.BadZipFile) as exc:
                            print(f"[Audio] Cannot read {member}: {exc}")
                            continue
                        if data is None:
                            continue
                        relative = member[len("assets/sounds/"):]
                        keys = [member]
                        # Bare names remain aliases only for top-level sounds.
                        if "/" not in relative:
                            keys.append(relative)
                        if self._load_sound_bytes_to_cache(keys, data):
                            count += 1
            except (OSError, zipfile.BadZipFile) as exc:
                print(f"[Audio] Cannot read sounds.fiopak: {exc}")
                return
            print(f"[Audio] Preloaded {count} sound files from sounds.fiopak.")
            return

        sound_dir = os.path.join(os.getcwd(), "assets", "sounds")
        if not os.path.isdir(sound_dir):
            print("[Audio] Warning: assets/sounds directory and sounds.fiopak not found.")
            return
        print("[Audio] Preloading sounds...")
        count = 0
        for f in os.listdir(sound_dir):
            if f.lower().endswith((".wav", ".mp3", ".ogg")):
                full_path = os.path.join(sound_dir, f)
                if self._load_sound_to_cache(f, full_path):
                    count += 1
        print(f"[Audio] Preloaded {count} sound files.")

    def _ensure_pygame_mixer(self) -> bool:
        """Initialize pygame mixer if it isn't already active.

        A failed attempt probes the audio stack for ~100 ms on the UI thread,
        and every sound request asks, so on a machine with no working device
        each gunshot used to stall a frame. The failure is remembered and the
        device retried at most every :data:`MIXER_RETRY_SECONDS`, so one
        plugged in later is still picked up.
        """
        if pygame.mixer.get_init():
            return True
        now = time.perf_counter()
        failed_at = getattr(self, '_mixer_failed_at', None)
        if failed_at is not None and now - failed_at < self.MIXER_RETRY_SECONDS:
            return False
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            print("[Audio] pygame.mixer late-initialized")
            self._mixer_failed_at = None
            return True
        except pygame.error as e:
            if failed_at is None:
                print(f"[Audio] pygame.mixer init failed: {e}")
            self._mixer_failed_at = now
            return False

    def _load_sound_to_cache(self, name, path):
        """Load a loose sound file into pygame mixer cache."""
        if not self._ensure_pygame_mixer():
            print(f"[Audio] Failed to load {name}: mixer not initialized")
            return False
        if name in self.sound_pool:
            return True
        try:
            sound = pygame.mixer.Sound(path)
            self.sound_pool[name] = sound
            return True
        except pygame.error as e:
            print(f"[Audio] Failed to load {name}: {e}")
            return False

    def _load_sound_bytes_to_cache(self, names, data):
        """Load archive-backed audio bytes into the cache under one or more keys."""
        if not self._ensure_pygame_mixer():
            print(f"[Audio] Failed to load {names[0]}: mixer not initialized")
            return False
        try:
            sound = pygame.mixer.Sound(file=io.BytesIO(data))
        except (pygame.error, TypeError, ValueError) as e:
            print(f"[Audio] Failed to load {names[0]} from archive: {e}")
            return False
        for name in names:
            self.sound_pool[name] = sound
        return True

    def _load_user_pack_sound(self, names, path):
        """Load the sound at *path* from an end user's own ``.fiopak`` pack
        in ``assets/`` (``engine.asset_archives``), when one holds it."""
        from engine.asset_archives import asset_bytes
        data = asset_bytes(path)
        return data is not None and self._load_sound_bytes_to_cache(names, data)

    def _get_sound_instance(self, name):
        """Get a pygame Sound by name, preferring the matching .fiopak if present.

        Explicit assets/sounds and assets/music paths retain their archive-relative
        names. Bare names keep the legacy assets/sounds lookup. An existing archive
        is authoritative; loose files are used only when that archive is absent.
        """
        from engine.audio_archives import read_archive_audio
        from engine.speaker_audio import resolve_speaker_sound, speaker_sound_path

        rel = speaker_sound_path(name) if '/' in str(name).replace('\\', '/') else None
        if rel is not None:
            if rel in self.sound_pool:
                return self.sound_pool[rel]
            if not self._ensure_pygame_mixer():
                return None
            folder = rel.split('/')[1]
            archive_path = os.path.join(os.getcwd(), "assets", f"{folder}.fiopak")
            if os.path.isfile(archive_path):
                try:
                    data = read_archive_audio(archive_path, rel, f"assets/{folder}")
                except (OSError, zipfile.BadZipFile) as exc:
                    print(f"[Audio] Cannot read {archive_path}: {exc}")
                    return None
                if data is not None and self._load_sound_bytes_to_cache([rel], data):
                    return self.sound_pool[rel]
                print(f"[Audio] Sound not found in {folder}.fiopak: {rel}")
                return None
            path = resolve_speaker_sound(rel, os.getcwd())
            if path and os.path.isfile(path) and self._load_sound_to_cache(rel, path):
                return self.sound_pool[rel]
            if path and self._load_user_pack_sound([rel], path):
                return self.sound_pool[rel]
            print(f"[Audio] Sound not found: {rel}")
            return None

        clean_name = os.path.basename(name)
        if clean_name in self.sound_pool:
            return self.sound_pool[clean_name]
        if not self._ensure_pygame_mixer():
            return None
        sound_archive = os.path.join(os.getcwd(), "assets", "sounds.fiopak")
        if os.path.isfile(sound_archive):
            member = f"assets/sounds/{clean_name}"
            try:
                data = read_archive_audio(sound_archive, member, "assets/sounds")
            except (OSError, zipfile.BadZipFile) as exc:
                print(f"[Audio] Cannot read sounds.fiopak: {exc}")
                return None
            if data is not None and self._load_sound_bytes_to_cache([clean_name, member], data):
                return self.sound_pool[clean_name]
            print(f"[Audio] Sound not found in sounds.fiopak: {clean_name}")
            return None
        path = os.path.join(os.getcwd(), 'assets', 'sounds', clean_name)
        if os.path.isfile(path):
            if self._load_sound_to_cache(clean_name, path):
                return self.sound_pool[clean_name]
        elif self._load_user_pack_sound([clean_name, f"assets/sounds/{clean_name}"], path):
            return self.sound_pool[clean_name]
        print(f"[Audio] Sound not found: {clean_name}")
        return None

    @staticmethod
    def _sound_radius_gain(distance, radius):
        """Linear falloff: full volume at the source, zero at the radius."""
        try:
            distance = max(0.0, float(distance))
            radius = max(0.0, float(radius))
        except (TypeError, ValueError):
            return 0.0
        if radius <= 0.0:
            return 0.0
        if distance >= radius:
            return 0.0
        return 1.0 - (distance / radius)

    def _spatial_sound_mix(self, position, radius=512.0, global_sound=False):
        """Return (gain, left, right) for a world-space sound source."""
        if global_sound or position is None:
            return 1.0, 1.0, 1.0
        try:
            source = np.asarray(position, dtype=np.float32)
            listener = np.asarray((
                float(self.view.camera.pos.x), float(self.view.camera.pos.y),
                float(self.view.camera.pos.z),
            ), dtype=np.float32)
            delta = source - listener
            distance = float(np.linalg.norm(delta))
        except (TypeError, ValueError, AttributeError):
            return 1.0, 1.0, 1.0

        gain = self._sound_radius_gain(distance, radius)
        if gain <= 0.0:
            return 0.0, 1.0, 1.0

        try:
            front = self.view.camera.get_front_vector()
            right_vec = glm.normalize(
                glm.cross(front, glm.vec3(0, 1, 0))
            )
            horizontal = glm.vec3(float(delta[0]), 0.0, float(delta[2]))
            if glm.length(horizontal) > 0.0001:
                horizontal = glm.normalize(horizontal)
                pan = float(glm.dot(horizontal, right_vec))
            else:
                pan = 0.0
        except Exception:
            pan = 0.0

        pan = max(-1.0, min(1.0, pan))
        angle = (pan + 1.0) * (math.pi / 4.0)
        return gain, math.cos(angle), math.sin(angle)

    def stop_all_sounds(self):
        """Immediately stop every mixer channel and cancel queued audio."""
        try:
            if pygame.mixer.get_init():
                pygame.mixer.stop()
        except (pygame.error, AttributeError):
            pass

        self.view.game_state.clear_sounds()

        speaker_channels = getattr(self, '_speaker_channels', None)
        if speaker_channels is not None:
            speaker_channels.clear()
        speaker_mix = getattr(self, '_speaker_mix', None)
        if speaker_mix is not None:
            speaker_mix.clear()

    def _process_sound_queue(self):
        """Drain queued sound requests and keep active speaker channels mixed.

        Speaker requests carry an action ('play'/'stop'), looping,
        position/radius for spatial speakers, and entity_id.
        Looping speakers remain tracked so their attenuation follows the
        listener as the player moves.
        """
        speaker_channels = getattr(self, "_speaker_channels", None)
        if speaker_channels is None:
            speaker_channels = self._speaker_channels = {}
        speaker_mix = getattr(self, "_speaker_mix", None)
        if speaker_mix is None:
            speaker_mix = self._speaker_mix = {}

        from engine.speaker_audio import is_music

        def apply_mix(channel, meta):
            gain, left, right = self._spatial_sound_mix(
                meta.get('position'),
                meta.get('radius', 512.0),
                bool(meta.get('global', False)),
            )
            volume = max(0.0, min(1.0, float(meta.get('volume', 1.0))))
            channel_volume = self.music_volume if meta.get('music') else self.sound_volume
            volume *= max(0.0, min(1.0, float(channel_volume)))
            if meta.get('position') is not None and not meta.get('global', False):
                channel.set_volume(
                    volume * gain * left,
                    volume * gain * right,
                )
            else:
                channel.set_volume(volume)

        for request in self.view.game_state.consume_sounds():
            action = request.get('action', 'play')
            entity_id = request.get('entity_id')

            if action == 'stop':
                channel = speaker_channels.pop(entity_id, None)
                speaker_mix.pop(entity_id, None)
                if channel is not None:
                    try:
                        channel.stop()
                    except Exception as exc:
                        print(f"[QtGameView] speaker stop failed: {exc}")
                continue

            sound_file = request.get('file')
            volume = request.get('volume', 1.0)
            if not sound_file:
                continue

            sound = self._get_sound_instance(sound_file)
            if not sound:
                continue

            loops = -1 if request.get('looping') else 0

            # If this speaker is already looping, stop the old channel first so
            # a re-trigger does not stack a second copy on top of itself.
            if entity_id is not None:
                prev = speaker_channels.pop(entity_id, None)
                speaker_mix.pop(entity_id, None)
                if prev is not None:
                    try:
                        prev.stop()
                    except Exception as exc:
                        print(f"[QtGameView] speaker restart stop failed: {exc}")

            channel = sound.play(loops=loops)
            if channel:
                meta = {
                    'position': request.get('position'),
                    'radius': request.get('radius', 512.0),
                    'global': bool(request.get('global', False)),
                    'volume': volume,
                    # Music and sound have their own volumes.
                    'music': is_music(sound_file),
                }
                apply_mix(channel, meta)

                # Track only entity-owned looping channels. One-shots get the
                # correct spatial mix at their start position.
                if entity_id is not None and loops != 0:
                    speaker_channels[entity_id] = channel
                    speaker_mix[entity_id] = meta

        # Re-mix active looping speakers every render frame so walking toward
        # or away from a speaker changes volume without re-triggering playback.
        for entity_id, channel in list(speaker_channels.items()):
            meta = speaker_mix.get(entity_id)
            if meta is not None:
                apply_mix(channel, meta)
