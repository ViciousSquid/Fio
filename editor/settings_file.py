"""settings.ini, read so that a damaged file cannot stop Fio starting.

settings.ini is read before the first window exists, and its values are read
with typed getters all over the editor. The stock parser raises on a file
with a stray line, a byte-order mark, a duplicated option or a truncated
write, and its getters raise on ``font_size = big``. In a windowed build
that was a start that did nothing at all: no window, no console, no message.

:class:`SettingsParser` is a ``ConfigParser`` (same interpolation, same file
format) that

* keeps the last of a duplicated section or option instead of refusing the
  file, and
* returns the caller's ``fallback`` when a value cannot be converted, and
  says so once per option.

:func:`read_settings` reads a file into one and never raises: a file that
cannot be parsed is set aside as ``settings.ini.corrupt`` and the defaults
apply. Both report through :data:`problems`, which the editor shows once the
console exists.
"""

import configparser
import os

#: Human-readable notes about settings that could not be used, oldest first.
problems = []


def _note(message):
    if message not in problems:
        problems.append(message)
        print(f"[Settings] {message}")


class SettingsParser(configparser.ConfigParser):
    """A ``ConfigParser`` whose typed getters fall back instead of raising."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('strict', False)
        super().__init__(*args, **kwargs)

    def _get_conv(self, section, option, conv, *, raw=False, vars=None,
                  fallback=configparser._UNSET, **kwargs):
        try:
            return super()._get_conv(section, option, conv, raw=raw, vars=vars,
                                     fallback=fallback, **kwargs)
        except (ValueError, configparser.InterpolationError) as error:
            if fallback is configparser._UNSET:
                raise
            _note(f"[{section}] {option} is not usable ({error}); "
                  f"using {fallback!r}")
            return fallback


def read_settings(config, path):
    """Read *path* into *config*; ``True`` if the file was read as written.

    A missing file is not a problem (the defaults apply). A file that cannot
    be decoded or parsed is retried as UTF-8 with a byte-order mark -- what
    Notepad's "UTF-8 with BOM" writes -- and, failing that, moved aside to
    ``<path>.corrupt`` so the next save starts clean and the user's values
    are still there to recover by hand.
    """
    if not path or not os.path.exists(path):
        return True
    error = None
    for encoding in (None, 'utf-8-sig'):
        try:
            config.read(path, encoding=encoding)
            return True
        except (configparser.Error, UnicodeError, ValueError) as caught:
            error = caught
            _clear(config)
        except OSError as caught:
            _note(f"{os.path.basename(path)} could not be read ({caught}); "
                  f"using the defaults")
            return False
    backup = path + '.corrupt'
    try:
        os.replace(path, backup)
        kept = f"; the file was kept as {os.path.basename(backup)}"
    except OSError:
        kept = ""
    first_line = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    _note(f"{os.path.basename(path)} is damaged ({first_line}); "
          f"using the defaults{kept}")
    return False


def _clear(config):
    for section in config.sections():
        config.remove_section(section)
