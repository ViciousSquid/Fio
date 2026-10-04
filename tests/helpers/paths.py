"""Where things are on disk.

Several tests read Fio's own source to assert a structural property — that a
hot loop does not convert an array per iteration, that two modules agree about
a constant instead of each hard-coding it.  Those need a path to a source file,
and a path spelt relative to the *test's* location breaks the moment the test
moves.  They go through here instead, which is anchored to the repository root.
"""

import os

#: Absolute path of the repository root (the directory holding ``main.py``).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def repo_path(*parts):
    """An absolute path to something in the repository."""
    return os.path.join(REPO_ROOT, *parts)


def read_source(*parts):
    """The text of a source file in the repository."""
    with open(repo_path(*parts), "r", encoding="utf-8") as handle:
        return handle.read()
