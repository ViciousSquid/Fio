import os
import posixpath
import json
import zipfile
from typing import Dict, List, Optional, Set


class PackageExporter:
    """
    Builds a .fiopak: the current map, every map it reaches through a level
    change, and the assets those maps reference, zipped with a metadata.json
    whose ``map_path`` names the entry map inside the archive.

    A package is a world container.  Everything written into it comes from
    inside the project: a map or asset reference that resolves outside
    ``<root>/maps`` / ``<root>/assets`` (an absolute path, or one climbing out
    with ``..``) is reported and skipped, never copied.
    """

    # Asset path patterns to scan for in map JSON
    ASSET_KEYS = {
        'textures': ['textures', 'texture', 'texture_path', 'sprite_2d', 'custom_idle',
                     'custom_shoot', 'custom_dead'],
        'models': ['model_path', 'mesh'],
        'sounds': ['sound_file', 'sound', 'audio']
    }

    # Property keys that name another map a level can change to.
    MAP_KEYS = ('target_map', 'map', 'next_map', 'next_level', 'target_level',
                'level_name', 'map_name')

    # Property naming the cutscene a LogicCamera plays (cutscenes/<name>.json).
    CUTSCENE_KEY = 'cutscene_file'

    # Where a bare asset filename may live under assets/.
    ASSET_SUBDIRS = ('textures', 'models', 'sounds', 'music', 'sprites', 'materials')

    def __init__(self, editor_state, root_dir: str):
        self.editor_state = editor_state
        self.root_dir = os.path.abspath(root_dir)
        self.errors: List[str] = []

    def export(self, output_path, metadata, current_map_path, parent_widget=None):
        """Export the current project as a .fiopak zip.

        Returns ``(success, errors)``.  A package written with some assets
        missing still succeeds; the misses are reported in *errors*.  The
        archive is assembled beside *output_path* and moved into place only
        once complete, so a failed export never damages an existing package.
        """
        import traceback

        temporary = None
        try:
            root = self.root_dir
            if not os.path.isdir(root):
                raise FileNotFoundError(f"Project root not found: {root}")

            current_map_abs = os.path.abspath(current_map_path)
            if not os.path.isfile(current_map_abs):
                raise FileNotFoundError(f"Current map not found: {current_map_abs}")

            # ---- 1. The maps: the current one and everything it links to ----
            all_maps = self._collect_map_dependencies(current_map_abs)
            start_map_rel = metadata.get('map_path')
            if start_map_rel:
                start_map_abs = os.path.join(root, start_map_rel)
                if os.path.isfile(start_map_abs):
                    self._collect_map_dependencies(start_map_abs, all_maps)
                else:
                    self.errors.append(f"Start map not found: {start_map_abs}")

            # ---- 2. Archive names, fixed before anything is written ----
            # The current map is named first so it keeps its own basename.
            map_archive_paths: Dict[str, str] = {}
            used = set()
            for map_file in [current_map_abs] + sorted(all_maps - {current_map_abs}):
                name, ext = os.path.splitext(os.path.basename(map_file))
                archive_path = f"maps/{name}{ext}"
                counter = 1
                while archive_path in used:
                    archive_path = f"maps/{name}_{counter}{ext}"
                    counter += 1
                used.add(archive_path)
                map_archive_paths[map_file] = archive_path

            manifest = dict(metadata)
            manifest['map_path'] = map_archive_paths[current_map_abs]

            # ---- 3. Cutscenes and referenced assets from every map ----
            referenced_assets: Set[str] = set()
            cutscene_entries: Dict[str, str] = {}   # arcname -> source
            for map_file in map_archive_paths:
                data = self._read_map(map_file)
                if data is None:
                    continue
                referenced_assets |= self._asset_references(data)
                for name in sorted(self._cutscene_references(data)):
                    src_path = self._resolve_cutscene(name, map_file)
                    if src_path is None:
                        continue
                    arcname = os.path.relpath(src_path, root).replace('\\', '/')
                    if arcname in cutscene_entries:
                        continue
                    cutscene_entries[arcname] = src_path
                    # Actors a cutscene spawns name their own sprites/models.
                    cutscene = self._read_map(src_path)
                    if cutscene is not None:
                        referenced_assets |= self._cutscene_asset_references(cutscene)

            asset_entries: Dict[str, str] = {}   # arcname -> source
            for asset_name in sorted(referenced_assets):
                src_path = self._resolve_asset(asset_name)
                if src_path is None:
                    continue
                arcname = os.path.relpath(src_path, root).replace('\\', '/')
                asset_entries.setdefault(arcname, src_path)

            # ---- 4. Assemble next to the destination, then swap it in ----
            out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
            temporary = os.path.join(
                out_dir, ".%s.tmp" % os.path.basename(output_path))
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.writestr('metadata.json', json.dumps(manifest, indent=2))
                for map_file, archive_path in map_archive_paths.items():
                    zf.write(map_file, archive_path)
                for arcname, src_path in sorted(cutscene_entries.items()):
                    zf.write(src_path, arcname)
                for arcname, src_path in sorted(asset_entries.items()):
                    zf.write(src_path, arcname)
            os.replace(temporary, output_path)
            temporary = None

            metadata['map_path'] = manifest['map_path']
            return True, list(self.errors)

        except Exception as e:
            error_msg = f"Export failed:\n{str(e)}\n\n{traceback.format_exc()}"
            self.errors.append(error_msg)
            if parent_widget:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.critical(parent_widget, "Export Error", error_msg)
            return False, self.errors
        finally:
            if temporary is not None and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _read_map(self, map_file) -> Optional[dict]:
        try:
            with open(map_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            self.errors.append(f"Could not read {map_file}: {e}")
            return None
        if not isinstance(data, dict):
            self.errors.append(f"Not a map document: {map_file}")
            return None
        return data

    def _asset_references(self, data: dict) -> Set[str]:
        """Every asset name a map's brushes and things refer to."""
        refs: Set[str] = set()
        for brush in data.get('brushes', []) or []:
            if not isinstance(brush, dict):
                continue
            textures = brush.get('textures')
            if isinstance(textures, dict):
                refs.update(t for t in textures.values() if t and isinstance(t, str))

        for thing in data.get('things', []) or []:
            if not isinstance(thing, dict):
                continue
            props = thing.get('properties')
            if not isinstance(props, dict):
                continue
            for asset_keys in self.ASSET_KEYS.values():
                for key in asset_keys:
                    val = props.get(key)
                    if val and isinstance(val, str):
                        refs.add(val)
        return refs

    def _cutscene_references(self, data: dict) -> Set[str]:
        """Every cutscene file a map's entities name."""
        refs: Set[str] = set()
        for entity in (data.get('things', []) or []) + (data.get('brushes', []) or []):
            if not isinstance(entity, dict):
                continue
            props = entity.get('properties')
            props = props if isinstance(props, dict) else entity
            val = props.get(self.CUTSCENE_KEY)
            if val and isinstance(val, str) and val.strip():
                refs.add(val.strip())
        return refs

    def _resolve_cutscene(self, name: str, map_file: str) -> Optional[str]:
        """The project's ``cutscenes/`` file *name* refers to, or None (reported).

        Resolved as the cutscene runtime does: against the project root, and
        only inside ``cutscenes/``.
        """
        cutscenes_dir = os.path.join(self.root_dir, 'cutscenes')
        rel = name.replace('\\', '/')
        candidate = os.path.join(self.root_dir, rel)
        where = os.path.basename(map_file)
        if os.path.isabs(rel) or not self._inside(candidate, cutscenes_dir):
            self.errors.append(
                f"Skipped cutscene outside cutscenes/: '{name}' referenced in {where}")
            return None
        if not os.path.isfile(candidate):
            self.errors.append(f"Missing cutscene: '{name}' referenced in {where}")
            return None
        return os.path.normpath(candidate)

    def _cutscene_asset_references(self, cutscene: dict) -> Set[str]:
        """Assets named by the entities a cutscene spawns as actors."""
        spawned = [row.get('definition') for row in cutscene.get('actors', []) or []
                   if isinstance(row, dict) and isinstance(row.get('definition'), dict)]
        return self._asset_references({'things': spawned})

    def _inside(self, path: str, base: str) -> bool:
        path = os.path.realpath(path)
        base = os.path.realpath(base)
        return path == base or path.startswith(base + os.sep)

    def _resolve_asset(self, asset_name: str) -> Optional[str]:
        """The project file an asset reference names, or None (reported)."""
        assets_dir = os.path.join(self.root_dir, 'assets')
        if os.path.isabs(asset_name):
            self.errors.append(f"Skipped asset outside the project: {asset_name}")
            return None
        rel = asset_name.replace('\\', '/')
        candidates = [os.path.join(assets_dir, sub, rel) for sub in self.ASSET_SUBDIRS]
        candidates.append(os.path.join(assets_dir, rel))
        candidates.append(os.path.join(self.root_dir, rel))
        # The runtime (sounds by basename from assets/sounds) and the player's
        # reader both fall back to the bare filename; so does the exporter.
        base = posixpath.basename(rel)
        if base and base != rel:
            candidates += [os.path.join(assets_dir, sub, base)
                           for sub in self.ASSET_SUBDIRS]
        escaped = False
        for src_path in candidates:
            if not self._inside(src_path, assets_dir):
                escaped = True
                continue
            if os.path.isfile(src_path):
                return os.path.normpath(src_path)
        if escaped and '..' in rel.split('/'):
            self.errors.append(f"Skipped asset outside the project: {asset_name}")
        else:
            self.errors.append(f"Missing asset: {asset_name}")
        return None

    def _collect_map_dependencies(self, map_path, collected=None):
        """*map_path* plus every project map it reaches by a level change."""
        if collected is None:
            collected = set()
        abs_path = os.path.abspath(map_path)
        if abs_path in collected or not os.path.isfile(abs_path):
            return collected
        collected.add(abs_path)
        data = self._read_map(abs_path)
        if data is None:
            return collected

        all_entities = (data.get('things', []) or []) + (data.get('brushes', []) or [])
        for entity in all_entities:
            if not isinstance(entity, dict):
                continue
            props = dict(entity)
            if isinstance(entity.get('properties'), dict):
                props.update(entity['properties'])

            target_map = None
            for key in self.MAP_KEYS:
                val = props.get(key)
                if val and isinstance(val, str) and val.strip():
                    target_map = val.strip()
                    break
            if not target_map:
                continue

            # As LevelChanger resolves it at run time: ".json" is optional.
            if not target_map.lower().endswith('.json'):
                target_map += '.json'
            if os.path.isabs(target_map):
                self.errors.append(
                    f"Skipped map outside the project: '{target_map}' "
                    f"referenced in {os.path.basename(abs_path)}")
                continue
            base_dir = os.path.dirname(abs_path)
            candidates = [
                os.path.join(self.root_dir, 'maps', target_map),
                os.path.join(self.root_dir, target_map),
                os.path.join(base_dir, target_map),
            ]
            for candidate in candidates:
                if self._inside(candidate, self.root_dir) and os.path.isfile(candidate):
                    self._collect_map_dependencies(candidate, collected)
                    break
            else:
                self.errors.append(f"Missing dependency: map '{target_map}' referenced in {os.path.basename(abs_path)}")
        return collected
