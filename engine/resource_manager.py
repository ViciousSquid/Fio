import zipfile
import io
import os

class ResourceManager:
    """
    Manages loading assets from the disk or from a .gamepackage zip archive.
    This abstraction allows the game to be run from either a loose file
    structure (in the editor) or a single package file (in play mode).
    """
    def __init__(self):
        self.package = None
        self.mode = 'disk'  # Can be 'disk' or 'package'

    def load_package(self, package_path):
        """Switches the manager to package mode."""
        try:
            self.package = zipfile.ZipFile(package_path, 'r')
            self.mode = 'package'
            print(f"Resource Manager: Switched to package mode for '{package_path}'")
            return True
        except (zipfile.BadZipFile, FileNotFoundError) as e:
            print(f"Error: Could not load package '{package_path}'. {e}")
            self.package = None
            self.mode = 'disk'
            return False

    def get_asset(self, path):
        """
        Gets an asset as a file-like bytes object.
        Returns a BytesIO object on success, None on failure.
        """
        # Normalize path for compatibility with zip archives (use forward slashes)
        path = path.replace('\\', '/')
        
        if self.mode == 'package' and self.package:
            try:
                # Read from the zip archive in memory
                with self.package.open(path) as file_in_zip:
                    return io.BytesIO(file_in_zip.read())
            except KeyError:
                print(f"Warning: Asset '{path}' not found in package.")
                return None
        else:
            # Read from the local disk
            if os.path.exists(path):
                return open(path, 'rb')
            else:
                print(f"Warning: Asset '{path}' not found on disk.")
                return None
    
    def get_text_asset(self, path):
        """Gets a text asset as a file-like text object (for JSON, shaders, etc.)."""
        asset_bytes_io = self.get_asset(path)
        if asset_bytes_io:
            return io.TextIOWrapper(asset_bytes_io, encoding='utf-8')
        return None

# Create a single, global instance to be used throughout the engine
resource_manager = ResourceManager()