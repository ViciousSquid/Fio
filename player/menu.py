"""Shared pause-menu geometry, used for both drawing and hit testing."""
def buttons(width, height, has_world=True):
    labels = [('resume', 'Resume'), ('open', 'Open world'), ('exit', 'Exit')]
    if not has_world:
        labels = labels[1:]
    button_width = min(400, width * .7)
    button_height = max(44, min(72, height * .11))
    left = (width - button_width) / 2
    top = height * .35
    return [(key, label, (left, top + index * (button_height + 14), button_width, button_height))
            for index, (key, label) in enumerate(labels)]


def hit_test(position, width, height, has_world=True):
    x, y = position
    for key, _, (left, top, w, h) in buttons(width, height, has_world):
        if left <= x <= left + w and top <= y <= top + h:
            return key
    return None
