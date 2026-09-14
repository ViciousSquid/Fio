"""Shared test helpers.

Not a test package — nothing here is collected.  It holds the pieces several
areas of the suite need: where the repository is, deterministic world/brush
factories, the lightweight fakes that stand in for the live editor and logic
thread, and the OpenGL context helpers the visual tier uses.
"""

from .paths import REPO_ROOT, repo_path, read_source  # noqa: F401
