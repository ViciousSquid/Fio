"""Where a Speaker's sound may come from: ``assets/sounds`` or ``assets/music``.

A Speaker's ``sound_file`` names a file inside one of those two folders,
stored project-relative (``assets/music/theme.ogg``). A bare file name, as
older maps hold, means ``assets/sounds``. Anything else -- another folder, an
absolute path, a path climbing out with ``..``, a file that is not audio -- is
refused, so a map cannot make a speaker play an arbitrary file.
"""

import os

#: The folders under ``assets/`` a Speaker plays from, as the Asset Browser's
#: Audio tab shows them.
SPEAKER_AUDIO_FOLDERS = ("sounds", "music")
AUDIO_EXTENSIONS = (".wav", ".ogg", ".mp3")


def speaker_sound_path(value):
    """``assets/<sounds|music>/...`` for *value*, or None when not allowed.

    Accepts ``assets/music/x.ogg``, ``music/x.ogg`` and a bare ``x.wav``
    (meaning ``assets/sounds/x.wav``), with forward or back slashes.
    """
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        return None
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if len(parts) == 1:
        parts = ["assets", "sounds"] + parts
    elif parts[0] in SPEAKER_AUDIO_FOLDERS:
        parts = ["assets"] + parts
    if len(parts) < 3 or parts[0] != "assets" or parts[1] not in SPEAKER_AUDIO_FOLDERS:
        return None
    if not parts[-1].lower().endswith(AUDIO_EXTENSIONS):
        return None
    return "/".join(parts)


def resolve_speaker_sound(value, root):
    """The file *value* names under *root*, or None when not allowed.

    The file need not exist; a symlink leading out of its folder is refused.
    """
    rel = speaker_sound_path(value)
    if rel is None:
        return None
    parts = rel.split("/")
    folder = os.path.realpath(os.path.join(root, parts[0], parts[1]))
    path = os.path.realpath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([folder, path]) != folder:
            return None
    except ValueError:
        return None
    return path


def list_speaker_audio(root, folder):
    """Audio files under ``assets/<folder>``, as ``assets/<folder>/...`` paths."""
    if folder not in SPEAKER_AUDIO_FOLDERS:
        return []
    base = os.path.join(root, "assets", folder)
    found = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.lower().endswith(AUDIO_EXTENSIONS):
                rel = os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")
                if speaker_sound_path(rel) == rel:
                    found.append(rel)
    return sorted(found, key=str.lower)
