import io
import pygame
from typing import Optional, Dict
from engine.resource_manager import ResourceManager


class AudioManager:
    """
    Audio provider that routes through ResourceManager for package compatibility.
    Falls back to filesystem paths when in directory mode.
    """
    
    def __init__(self):
        self._sounds: Dict[str, pygame.mixer.Sound] = {}
        self._rm = ResourceManager()
        
        # Ensure pygame mixer is initialized before any Sound operations
        if not pygame.mixer.get_init():
            try:
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
                print("[AudioManager] pygame.mixer initialized")
            except pygame.error as e:
                print(f"[AudioManager] pygame.mixer init failed: {e}")
    
    def _ensure_mixer(self) -> bool:
        """Lazy-init mixer if something else quit it."""
        if pygame.mixer.get_init():
            return True
        try:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            return True
        except pygame.error as e:
            print(f"[AudioManager] pygame.mixer re-init failed: {e}")
            return False
    
    def load_sound(self, relative_path: str) -> Optional[pygame.mixer.Sound]:
        """
        Load a sound effect from current ResourceManager mount.
        Uses BytesIO streaming when in ZIP package mode.
        """
        if not self._ensure_mixer():
            print(f"[AudioManager] Cannot load '{relative_path}': mixer not initialized")
            return None
        
        # Check cache
        if relative_path in self._sounds:
            return self._sounds[relative_path]
        
        if self._rm.is_package_mode():
            # ZIP mode: stream from bytes
            stream = self._rm.get_asset_stream(relative_path)
            if stream is None:
                print(f"[AudioManager] Sound not found in package: {relative_path}")
                return None
            
            try:
                sound = pygame.mixer.Sound(file=stream)
                self._sounds[relative_path] = sound
                return sound
            except pygame.error as e:
                print(f"[AudioManager] Failed to decode sound: {e}")
                return None
        else:
            # Directory mode: use filesystem path directly
            full_path = self._rm.resolve_path(relative_path)
            if full_path is None:
                print(f"[AudioManager] Sound not found: {relative_path}")
                return None
            
            try:
                sound = pygame.mixer.Sound(full_path)
                self._sounds[relative_path] = sound
                return sound
            except pygame.error as e:
                print(f"[AudioManager] Failed to load sound: {e}")
                return None
    
    def play_sound(self, relative_path: str, volume: float = 1.0) -> bool:
        """Convenience: load and play a sound effect immediately."""
        sound = self.load_sound(relative_path)
        if sound:
            sound.set_volume(volume)
            sound.play()
            return True
        return False
    
    def clear_cache(self) -> None:
        """Release all loaded sounds (call on package switch)."""
        self._sounds.clear()