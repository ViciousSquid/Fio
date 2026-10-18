"""APK-only entry point; the desktop's main.py is never changed."""
import os

os.environ.setdefault("PYOPENGL_PLATFORM", "egl")

from player.main import main

if __name__ == "__main__":
    raise SystemExit(main([]))
