"""A virtual pointer for a wlroots compositor (sway), driven over stdin.

Headless sway has no pointer device, so nothing can click; ``wlrctl`` adds
one for a single command and removes it before the client has bound to it.
This keeps one virtual pointer (wlr-virtual-pointer-unstable-v1) alive for
as long as it runs. Commands, one per line:

    move X Y W H    put the pointer at (X, Y) of a W x H output layout
    press | release | click     the left button

It prints ``ready`` once the pointer exists and ``ok`` after each command has
reached the compositor. Run as its own process: its Wayland connection is
independent of the application under test. Needs ``pywayland``.
"""

import importlib
import os
import sys
import tempfile
import time

PROTOCOL = """<?xml version="1.0" encoding="UTF-8"?>
<protocol name="wlr_virtual_pointer_unstable_v1">
  <interface name="zwlr_virtual_pointer_v1" version="2">
    <enum name="error">
      <entry name="invalid_axis" value="0"/>
      <entry name="invalid_axis_source" value="1"/>
    </enum>
    <request name="motion">
      <arg name="time" type="uint"/>
      <arg name="dx" type="fixed"/>
      <arg name="dy" type="fixed"/>
    </request>
    <request name="motion_absolute">
      <arg name="time" type="uint"/>
      <arg name="x" type="uint"/>
      <arg name="y" type="uint"/>
      <arg name="x_extent" type="uint"/>
      <arg name="y_extent" type="uint"/>
    </request>
    <request name="button">
      <arg name="time" type="uint"/>
      <arg name="button" type="uint"/>
      <arg name="state" type="uint"/>
    </request>
    <request name="axis">
      <arg name="time" type="uint"/>
      <arg name="axis" type="uint"/>
      <arg name="value" type="fixed"/>
    </request>
    <request name="frame"/>
    <request name="axis_source">
      <arg name="axis_source" type="uint"/>
    </request>
    <request name="axis_stop">
      <arg name="time" type="uint"/>
      <arg name="axis" type="uint"/>
    </request>
    <request name="axis_discrete" since="2">
      <arg name="time" type="uint"/>
      <arg name="axis" type="uint"/>
      <arg name="value" type="fixed"/>
      <arg name="discrete" type="int"/>
    </request>
    <request name="destroy" type="destructor" since="1"/>
  </interface>
  <interface name="zwlr_virtual_pointer_manager_v1" version="2">
    <request name="create_virtual_pointer">
      <arg name="seat" type="object" interface="wl_seat" allow-null="true"/>
      <arg name="id" type="new_id" interface="zwlr_virtual_pointer_v1"/>
    </request>
    <request name="destroy" type="destructor" since="1"/>
    <request name="create_virtual_pointer_with_output" since="2">
      <arg name="seat" type="object" interface="wl_seat" allow-null="true"/>
      <arg name="output" type="object" interface="wl_output" allow-null="true"/>
      <arg name="id" type="new_id" interface="zwlr_virtual_pointer_v1"/>
    </request>
  </interface>
</protocol>
"""

BTN_LEFT = 0x110


def _load_protocol():
    """Generate the protocol's Python bindings with pywayland's scanner."""
    from pywayland.scanner import Protocol
    out = tempfile.mkdtemp(prefix='fio-wlr-vp-')
    xml = os.path.join(out, 'wlr-virtual-pointer-unstable-v1.xml')
    with open(xml, 'w', encoding='utf-8') as stream:
        stream.write(PROTOCOL)
    protocol = Protocol.parse_file(xml)
    imports = {iface.name: protocol.name for iface in protocol.interface}
    # wl_seat / wl_output come from pywayland's own core protocol.
    imports.update({'wl_seat': 'wayland', 'wl_output': 'wayland'})
    protocol.output(out, imports)
    # The scanner writes package-relative imports for the core protocol;
    # point them at pywayland's own.
    module = os.path.join(out, protocol.name + '.py')
    with open(module, encoding='utf-8') as stream:
        source = stream.read()
    source = source.replace('from .wayland import', 'from pywayland.protocol.wayland import')
    with open(module, 'w', encoding='utf-8') as stream:
        stream.write(source)
    sys.path.insert(0, out)
    return importlib.import_module(protocol.name)


def main():
    from pywayland.client import Display
    from pywayland.protocol.wayland import WlSeat
    proto = _load_protocol()
    Manager = proto.ZwlrVirtualPointerManagerV1

    display = Display()
    display.connect()
    found = {}

    def on_global(registry, name, interface, version):
        if interface == 'wl_seat' and 'seat' not in found:
            found['seat'] = registry.bind(name, WlSeat, 1)
        elif interface == Manager.name:
            found['manager'] = registry.bind(name, Manager, 1)

    registry = display.get_registry()
    registry.dispatcher['global'] = on_global
    display.roundtrip()
    if 'manager' not in found:
        print('error: the compositor has no zwlr_virtual_pointer_manager_v1', flush=True)
        return 1
    pointer = found['manager'].create_virtual_pointer(found.get('seat'))
    display.roundtrip()
    print('ready', flush=True)

    def stamp():
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    for line in sys.stdin:
        words = line.split()
        if not words:
            continue
        if words[0] == 'move':
            x, y, w, h = (int(v) for v in words[1:5])
            pointer.motion_absolute(stamp(), x, y, w, h)
            pointer.frame()
        elif words[0] in ('press', 'release', 'click'):
            states = {'press': [1], 'release': [0], 'click': [1, 0]}[words[0]]
            for state in states:
                pointer.button(stamp(), BTN_LEFT, state)
                pointer.frame()
                display.roundtrip()
                time.sleep(0.03)
        elif words[0] == 'quit':
            break
        display.roundtrip()
        print('ok', flush=True)
    pointer.destroy()
    display.roundtrip()
    display.disconnect()
    return 0


if __name__ == '__main__':
    sys.exit(main())
