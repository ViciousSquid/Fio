import os
import json
import zipfile
import shutil
import traceback
from pathlib import Path
from typing import Set, List, Dict, Optional, Tuple
from collections import deque

from PyQt5.QtWidgets import QProgressDialog, QMessageBox, QApplication
from PyQt5.QtCore import Qt


class PackageExporter:
    """
    Handles .fiopak creation: metadata collection, asset crawling,
    recursive map dependency resolution, and ZIP assembly.
    """
    
    # Asset path patterns to scan for in map JSON
    ASSET_KEYS = {
        'textures': ['textures', 'texture', 'texture_path', 'sprite_2d', 'custom_idle', 
                     'custom_shoot', 'custom_dead'],
        'models': ['model_path', 'mesh'],
        'sounds': ['sound_file', 'sound', 'audio']
    }
    
    def __init__(self, editor_state, root_dir: str):
        self.editor_state = editor_state
        self.root_dir = os.path.abspath(root_dir)
        self._discovered_maps: Set[str] = set()
        self._discovered_assets: Dict[str, Set[str]] = {
            'textures': set(),
            'models': set(),
            'sounds': set()
        }
        self.errors: List[str] = []


    def export(self, output_path, metadata, current_map_path, parent_widget=None):
        """
        Export the current project as a .fiopak zip.
        Now safe for Nuitka builds: all paths are absolute, errors are caught and shown.
        """
        import zipfile
        import json
        import os
        import traceback
        from PyQt5.QtWidgets import QMessageBox

        try:
            # ---- 1. Normalise all paths to absolute ----
            root = os.path.abspath(self.root_dir)
            current_map_abs = os.path.abspath(current_map_path)

            # Ensure root directory exists
            if not os.path.isdir(root):
                raise FileNotFoundError(f"Project root not found: {root}")

            # ---- 2. Collect all map dependencies ----
            start_map_rel = metadata.get('map_path')
            all_maps = set()

            # Resolve start map relative to root_dir
            if start_map_rel:
                start_map_abs = os.path.join(self.root_dir, start_map_rel)
                if os.path.isfile(start_map_abs):
                    all_maps = self._collect_map_dependencies(start_map_abs)
                else:
                    self.errors.append(f"Start map not found: {start_map_abs}")

            # ALWAYS include the current map if it exists (it is the authoritative source)
            if os.path.isfile(current_map_abs):
                all_maps.add(current_map_abs)
            elif current_map_abs:
                self.errors.append(f"Current map not found: {current_map_abs}")

            if not all_maps:
                tried = f"start_map={start_map_rel}, current_map={current_map_abs}"
                raise Exception(f"No map files found to export. (Tried: {tried})")

            # ---- 3. Gather referenced assets from ALL maps ----
            referenced_assets = set()
            for map_file in all_maps:
                try:
                    with open(map_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    self.errors.append(f"Could not read {map_file}: {e}")
                    continue

                # Scan brushes for textures
                for brush in data.get('brushes', []):
                    textures = brush.get('textures', {})
                    for face, tex in textures.items():
                        if tex and isinstance(tex, str):
                            referenced_assets.add(tex)

                # Scan things for model paths, sprites, sounds, etc.
                for thing in data.get('things', []):
                    if not isinstance(thing, dict):
                        continue
                    props = thing.get('properties', {})
                    for asset_keys in self.ASSET_KEYS.values():
                        for key in asset_keys:
                            val = props.get(key)
                            if val and isinstance(val, str):
                                referenced_assets.add(val)

            # ---- 4. Build the .fiopak zip ----
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Write metadata.json
                zf.writestr('metadata.json', json.dumps(metadata, indent=2))

                # Write each map into maps/ folder with clean archive paths
                map_archive_paths = {}
                for map_file in all_maps:
                    basename = os.path.basename(map_file)
                    archive_path = f"maps/{basename}"
                    # Handle name collisions
                    counter = 1
                    while archive_path in map_archive_paths.values():
                        name, ext = os.path.splitext(basename)
                        archive_path = f"maps/{name}_{counter}{ext}"
                        counter += 1
                    map_archive_paths[map_file] = archive_path
                    zf.write(map_file, archive_path)

                # Ensure metadata points to the correct archive-internal path
                if current_map_abs in map_archive_paths:
                    metadata['map_path'] = map_archive_paths[current_map_abs]
                elif all_maps:
                    first_map = next(iter(all_maps))
                    metadata['map_path'] = map_archive_paths.get(first_map, 'maps/map.json')

                # Write referenced assets
                assets_dir = os.path.join(root, 'assets')
                for asset_name in referenced_assets:
                    found = False
                    # Try multiple candidate locations
                    candidates = []
                    if os.path.isabs(asset_name):
                        candidates.append(asset_name)
                    else:
                        for sub in ('textures', 'models', 'sounds', 'sprites', 'materials'):
                            candidates.append(os.path.join(assets_dir, sub, asset_name))
                        candidates.append(os.path.join(assets_dir, asset_name))

                    for src_path in candidates:
                        if os.path.isfile(src_path):
                            arcname = os.path.relpath(src_path, root).replace('\\', '/')
                            zf.write(src_path, arcname)
                            found = True
                            break

                    if not found:
                        self.errors.append(f"Missing asset: {asset_name}")

            # ---- 4.5 Bundle plugins the maps depend on ----
            # A .fiopak must carry the plugins its maps use, or it will only load
            # on the machine that built it. This is a first-class step of the
            # export: inject the required plugin code and assets into the archive
            # (see plugins.packaging.augment_fiopak), then drop the cosmetic
            # "Missing asset" warnings for files the plugin bundle actually
            # supplied — plugin assets live outside the assets/ tree scanned above.
            bundled = self._bundle_plugins(output_path)
            if bundled:
                norm = {a.lower().lstrip("/") for a in bundled}
                self.errors = [e for e in self.errors
                               if not self._is_bundled_missing(e, norm)]

            # ---- 5. Report results ----
            if self.errors:
                return True, self.errors   # package created but with warnings
            return True, []

        except Exception as e:
            error_msg = f"Export failed:\n{str(e)}\n\n{traceback.format_exc()}"
            self.errors.append(error_msg)
            if parent_widget:
                QMessageBox.critical(parent_widget, "Export Error", error_msg)
            return False, self.errors

    @staticmethod
    def _log(message: str):
        """Route an export message to the editor debug console, else stdout."""
        try:
            from editor.debug_console import debug_log
            debug_log("Plugins", message)
        except Exception:
            print(f"[Plugins] {message}")

    def _bundle_plugins(self, output_path) -> Set[str]:
        """Inject the plugins the exported maps depend on into the .fiopak.

        First-class, native step of the export: it makes the package
        self-contained and portable. Guarded so a build without the optional
        plugin system (or a headless/tool context where it can't import) simply
        skips bundling rather than failing the export.

        Returns the set of archive paths the plugin bundle added — empty when the
        maps use no plugin entities or the plugin system is unavailable.
        """
        try:
            from plugins.packaging import augment_fiopak
        except Exception:
            return set()  # plugin system not present in this build

        try:
            summary = augment_fiopak(output_path, log=self._log)
        except Exception as exc:
            self.errors.append(f"Plugin packaging failed: {exc}")
            return set()

        plugins = summary.get("plugins") or []
        if plugins:
            self._log("bundled plugin(s): " + ", ".join(plugins))
        return set(summary.get("added_paths", set()))

    @staticmethod
    def _is_bundled_missing(error_text, bundled_norm) -> bool:
        """True if *error_text* is a 'Missing asset: <p>' now supplied by a plugin."""
        marker = "Missing asset:"
        if marker not in str(error_text):
            return False
        asset = (str(error_text).split(marker, 1)[1].strip()
                 .replace("\\", "/").lower().lstrip("/"))
        if asset in bundled_norm:
            return True
        # Also match by basename in case the reference used a bare filename.
        base = asset.rsplit("/", 1)[-1]
        return any(b.rsplit("/", 1)[-1] == base for b in bundled_norm)

    def _crawl_campaign(self, start_map_path: str) -> None:
        """
        Breadth-first crawl of all maps reachable via LevelChanger entities.
        Populates self._discovered_maps and self._discovered_assets.
        """
        queue = deque([start_map_path])
        
        while queue:
            map_rel_path = queue.popleft()
            if map_rel_path in self._discovered_maps:
                continue
            
            self._discovered_maps.add(map_rel_path)
            
            # Load map JSON
            map_full_path = os.path.join(self.root_dir, map_rel_path)
            if not os.path.exists(map_full_path):
                self.errors.append(f"Map file not found: {map_rel_path}")
                continue
            
            try:
                with open(map_full_path, 'r', encoding='utf-8') as f:
                    map_data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                self.errors.append(f"Failed to parse {map_rel_path}: {e}")
                continue
            
            # Extract assets from this map
            self._extract_map_assets(map_data)
            
            # Find LevelChangers to discover next maps
            for thing in map_data.get('things', []):
                if thing.get('type') == 'LevelChanger':
                    target = thing.get('properties', {}).get('target_map', '')
                    if target:
                        # Normalize to maps/ relative path
                        if not target.startswith('maps/'):
                            target = f"maps/{target}"
                        if not target.endswith('.json'):
                            target += '.json'
                        queue.append(target)


    def _collect_map_dependencies(self, map_path, collected=None):
        import json, os
        if collected is None:
            collected = set()
        abs_path = os.path.abspath(map_path)
        if abs_path in collected or not os.path.isfile(abs_path):
            return collected
        collected.add(abs_path)
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.errors.append(f"Could not scan {abs_path}: {e}")
            return collected

        all_entities = data.get('things', []) + data.get('brushes', [])
        for entity in all_entities:
            if not isinstance(entity, dict):
                continue
            props = dict(entity)
            if 'properties' in entity and isinstance(entity['properties'], dict):
                props.update(entity['properties'])

            target_map = None
            for key in ('target_map', 'map', 'next_map', 'next_level', 'target_level', 'level_name', 'map_name'):
                val = props.get(key)
                if val and isinstance(val, str) and val.strip():
                    target_map = val.strip()
                    break
            if not target_map:
                continue

            # Resolve relative map path to absolute
            candidates = []
            if os.path.isabs(target_map):
                candidates.append(target_map)
            else:
                base_dir = os.path.dirname(abs_path)
                candidates.extend([
                    os.path.join(self.root_dir, 'maps', target_map),
                    os.path.join(self.root_dir, target_map),
                    os.path.join(base_dir, target_map)
                ])
            for candidate in candidates:
                if os.path.isfile(candidate):
                    self._collect_map_dependencies(candidate, collected)
                    break
            else:
                self.errors.append(f"Missing dependency: map '{target_map}' referenced in {os.path.basename(abs_path)}")
        return collected
    
    def _extract_map_assets(self, map_data: dict) -> None:
        """Scan map data structure for all asset references."""
        def scan_value(value, context: str = ''):
            if isinstance(value, dict):
                for k, v in value.items():
                    # Check if this key matches known asset patterns
                    for asset_type, keys in self.ASSET_KEYS.items():
                        if k in keys and isinstance(v, str) and v:
                            self._discovered_assets[asset_type].add(v)
                    # Recurse into nested structures
                    scan_value(v, f"{context}.{k}")
            elif isinstance(value, list):
                for item in value:
                    scan_value(item, context)
            elif isinstance(value, str) and value:
                # Heuristic: catch file extensions in string values
                lower = value.lower()
                if any(lower.endswith(ext) for ext in ['.png', '.jpg', '.tga', '.bmp']):
                    self._discovered_assets['textures'].add(value)
                elif lower.endswith('.obj'):
                    self._discovered_assets['models'].add(value)
                elif any(lower.endswith(ext) for ext in ['.wav', '.ogg', '.mp3']):
                    self._discovered_assets['sounds'].add(value)
        
        scan_value(map_data)
    
    def _resolve_asset_paths(self) -> Dict[str, List[Tuple[str, str]]]:
        """
        Convert discovered relative paths to (archive_path, filesystem_path) tuples.
        Returns organized dict for ZIP assembly.
        """
        manifest = {'maps': [], 'assets': {'textures': [], 'models': [], 'sounds': []}}
        
        # Maps
        for map_rel in sorted(self._discovered_maps):
            # Safety: ensure map path is relative, never absolute
            clean_map_rel = map_rel.replace("\\", "/")
            if os.path.isabs(clean_map_rel):
                clean_map_rel = os.path.relpath(clean_map_rel, self.root_dir)
                clean_map_rel = clean_map_rel.replace("\\", "/")
            full = os.path.join(self.root_dir, clean_map_rel)
            if os.path.exists(full):
                manifest['maps'].append((clean_map_rel, full))
        
        # Assets by type
        for asset_type, paths in self._discovered_assets.items():
            for rel_path in sorted(paths):
                # Normalize path to assets/ subdirectory
                clean_path = rel_path.replace('\\', '/').lstrip('/')
                if not clean_path.startswith('assets/'):
                    archive_path = f"assets/{asset_type}/{os.path.basename(clean_path)}"
                else:
                    archive_path = clean_path
                
                full_path = os.path.join(self.root_dir, clean_path)
                if not os.path.exists(full_path):
                    # Try alternative resolution
                    alt_path = os.path.join(self.root_dir, archive_path)
                    if os.path.exists(alt_path):
                        full_path = alt_path
                    else:
                        self.errors.append(f"Asset not found: {rel_path}")
                        continue
                
                manifest['assets'][asset_type].append((archive_path, full_path))
        
        return manifest
    
    def _assemble_package(self, output_path: str, metadata: dict,
                         asset_manifest: dict, progress: QProgressDialog) -> bool:
        """Build the final ZIP archive with all contents."""
        try:
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Write manifest
                manifest_json = json.dumps(metadata, indent=2, ensure_ascii=False)
                zf.writestr('manifest.json', manifest_json)
                
                total_items = (len(asset_manifest['maps']) + 
                              sum(len(v) for v in asset_manifest['assets'].values()))
                processed = 0
                
                # Write maps
                for archive_path, fs_path in asset_manifest['maps']:
                    zf.write(fs_path, archive_path)
                    processed += 1
                    if processed % 5 == 0:
                        progress.setValue(50 + int(40 * processed / max(total_items, 1)))
                        QApplication.processEvents()
                
                # Write assets
                for asset_type, items in asset_manifest['assets'].items():
                    for archive_path, fs_path in items:
                        zf.write(fs_path, archive_path)
                        processed += 1
                
                # Copy banner if specified
                banner_src = metadata.get('banner_source_path', '')
                if banner_src and os.path.exists(banner_src):
                    banner_ext = os.path.splitext(banner_src)[1]
                    zf.write(banner_src, f"assets/package_banner{banner_ext}")
            
            return True
            
        except (IOError, OSError, zipfile.BadZipFile) as e:
            self.errors.append(f"Package assembly failed: {e}")
            return False