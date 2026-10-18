"""Uniform locations live only for the current GL context/program links."""
_locations = {}


def clear():
    _locations.clear()


def location(gl, program, name):
    key = (int(program), name)
    if key not in _locations:
        _locations[key] = gl.glGetUniformLocation(program, name)
    return _locations[key]
