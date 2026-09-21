import os
import json
import zipfile
import io
from pathlib import Path
from typing import Optional, Dict, List, Union, BinaryIO


class ResourceManager:
    """
    Unified asset provider that transparently serves files from either
    a standard directory tree or a mounted .fiopak ZIP archive.
    
    Singleton pattern ensures consistent mount state across engine modules.
    """
    
    _instance: Optional['ResourceManager'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        # Filesystem mode state
        self._root_dir: Optional[str] = None
        
        # ZIP archive mode state
        self._zip_handle: Optional[zipfile.ZipFile] = None
        self._zip_path: Optional[str] = None
        
        # Asset cache for hot assets (textures, sounds loaded as bytes)
        self._asset_cache: Dict[str, bytes] = {}
        self._text_cache: Dict[str, str] = {}
        
        # Manifest data when in package mode
        self._manifest: Optional[dict] = None
    
    # ------------------------------------------------------------------
    # Mounting Operations
    # ------------------------------------------------------------------
    
    def mount_directory(self, root_dir: str) -> None:
        """Traditional filesystem mode — editor default."""
        self._cleanup_zip()
        self._root_dir = os.path.abspath(root_dir)
        self._manifest = None
        print(f"[ResourceManager] Mounted directory: {self._root_dir}")
    
    def mount_package(self, package_path: str) -> bool:
        """
        Mount a .fiopak ZIP archive for in-memory asset streaming.
        Returns True on successful mount with valid manifest and an existing
        start map.
        """
        self._cleanup_zip()
        
        if not os.path.exists(package_path):
            print(f"[ResourceManager] Package not found: {package_path}")
            return False
        
        try:
            self._zip_handle = zipfile.ZipFile(package_path, 'r')
            self._zip_path = os.path.abspath(package_path)
            
            # Validate and load manifest
            manifest_data = self._load_text_internal('manifest.json')
            if manifest_data is None:
                print("[ResourceManager] Invalid package: manifest.json missing")
                self._cleanup_zip()
                return False
            
            self._manifest = json.loads(manifest_data)
            print(f"[ResourceManager] Mounted package: {package_path}")
            print(f"  Title: {self._manifest.get('title', 'Untitled')}")
            
            # Verify / repair start map existence
            if not self._ensure_start_map_exists():
                print("[ResourceManager] Package has no usable start map – mount failed")
                self._cleanup_zip()
                return False
            
            return True
            
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as e:
            print(f"[ResourceManager] Failed to mount package: {e}")
            self._cleanup_zip()
            return False
    
    def _cleanup_zip(self) -> None:
        """Release ZIP handle and clear caches."""
        if self._zip_handle:
            self._zip_handle.close()
            self._zip_handle = None
        self._zip_path = None
        self._manifest = None
        self._asset_cache.clear()
        self._text_cache.clear()
    
    def _ensure_start_map_exists(self) -> bool:
        """
        Check that the start map referenced in the manifest actually exists
        inside the ZIP. If not, try to find any .json map file in the archive
        and update the manifest accordingly. Returns True if a valid start map
        is available, False otherwise.
        """
        # Determine the current start map key (common names used by the editor)
        possible_keys = ('map_path', 'start_map', 'main_map')
        start_map = None
        for key in possible_keys:
            if key in self._manifest:
                start_map = self._manifest[key]
                if start_map:
                    break
        
        # Normalise path (strip leading slashes, force forward slashes)
        if start_map:
            start_map = start_map.replace('\\', '/').lstrip('/')
            # Try to read it from the ZIP
            try:
                self._zip_handle.getinfo(start_map)
                return True
            except KeyError:
                print(f"[ResourceManager] Warning: start map '{start_map}' not found in package")
                start_map = None
        
        # No valid start map – try to find any .json map file in the archive
        map_files = [name for name in self._zip_handle.namelist()
                     if name.endswith('.json') and not name.startswith('manifest.json')]
        if not map_files:
            print("[ResourceManager] No map file (.json) found in package")
            return False
        
        # Pick the first map file (alphabetically)
        candidate = sorted(map_files)[0]
        print(f"[ResourceManager] Using auto‑detected map: {candidate}")
        
        # Update the manifest with the detected map (store under a standard key)
        self._manifest['map_path'] = candidate
        return True
    
    # ------------------------------------------------------------------
    # Asset Access API
    # ------------------------------------------------------------------
    
    def get_asset(self, relative_path: str) -> Optional[bytes]:
        """
        Retrieve raw bytes for any asset (images, models, sounds).
        Returns None if asset not found in current mount context.
        """
        normalized = self._normalize_path(relative_path)
        
        # Check cache first
        if normalized in self._asset_cache:
            return self._asset_cache[normalized]
        
        data = self._load_bytes_internal(normalized)
        if data is not None:
            self._asset_cache[normalized] = data
        return data
    
    def get_text_asset(self, relative_path: str) -> Optional[str]:
        """
        Retrieve decoded text for JSON, OBJ, MTL, shader files.
        Auto-detects UTF-8 encoding with BOM fallback.
        """
        normalized = self._normalize_path(relative_path)
        
        if normalized in self._text_cache:
            return self._text_cache[normalized]
        
        data = self._load_text_internal(normalized)
        if data is not None:
            self._text_cache[normalized] = data
        return data
    
    def get_asset_stream(self, relative_path: str) -> Optional[BinaryIO]:
        """
        Return a file-like object (io.BytesIO) for streaming consumers.
        Essential for pygame.mixer.Sound initialization from memory.
        """
        data = self.get_asset(relative_path)
        if data is None:
            return None
        return io.BytesIO(data)
    
    # ------------------------------------------------------------------
    # Internal Loaders
    # ------------------------------------------------------------------
    
    def _load_bytes_internal(self, normalized_path: str) -> Optional[bytes]:
        """Raw byte loading from current mount context."""
        if self._zip_handle:
            # ZIP mode: direct archive read
            # Try the path as-is first
            try:
                return self._zip_handle.read(normalized_path)
            except KeyError:
                pass
            # Fallback: try without assets/ prefix (packages may store files relative to assets/)
            if normalized_path.startswith('assets/'):
                try:
                    return self._zip_handle.read(normalized_path[7:])
                except KeyError:
                    pass
            return None
        elif self._root_dir:
            # Filesystem mode: standard file read
            full_path = os.path.join(self._root_dir, normalized_path)
            if os.path.exists(full_path):
                with open(full_path, 'rb') as f:
                    return f.read()
        return None
    
    def _load_text_internal(self, normalized_path: str) -> Optional[str]:
        """Text loading with encoding detection."""
        data = self._load_bytes_internal(normalized_path)
        if data is None:
            return None
        
        # Try UTF-8 first, then UTF-8-SIG (BOM), then latin-1 fallback
        for encoding in ('utf-8', 'utf-8-sig', 'latin-1'):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return None
    
    @staticmethod
    def _normalize_path(path: str) -> str:
        """Convert to forward-slash relative path, strip leading slashes."""
        path = path.replace('\\', '/').lstrip('/')
        return path
    
    # ------------------------------------------------------------------
    # Package-Specific Queries
    # ------------------------------------------------------------------
    
    def get_manifest(self) -> Optional[dict]:
        """Access package manifest when in package mode."""
        return self._manifest
    
    def get_start_map(self) -> Optional[str]:
        """Resolve the starting map path from manifest."""
        if not self._manifest:
            return None
        # Try common keys in order of preference
        for key in ('map_path', 'start_map', 'main_map'):
            if key in self._manifest:
                return self._manifest[key]
        return None
    
    def list_package_contents(self) -> List[str]:
        """Return all file paths in mounted package."""
        if self._zip_handle:
            return self._zip_handle.namelist()
        return []
    
    def is_package_mode(self) -> bool:
        """True when a .fiopak is mounted (not directory mode)."""
        return self._zip_handle is not None
    
    # ------------------------------------------------------------------
    # Legacy Compatibility
    # ------------------------------------------------------------------
    
    def resolve_path(self, relative_path: str) -> Optional[str]:
        """
        For consumers that absolutely need a filesystem path (rare).
        Returns None in package mode — forces callers to use byte APIs.
        """
        if self._root_dir:
            full = os.path.join(self._root_dir, relative_path)
            return full if os.path.exists(full) else None
        return None  # Cannot provide real path when ZIP-mounted