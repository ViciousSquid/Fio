"""Build a small universal package through the existing desktop exporter."""
import argparse
import json
from pathlib import Path
import struct
import tempfile
import zlib


def make_fixture(output):
    from editor.package_exporter import PackageExporter
    def png():
        def chunk(kind, data):
            return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / 'maps/sub').mkdir(parents=True)
        (root / 'assets/models/demo').mkdir(parents=True)
        (root / 'assets/textures').mkdir(parents=True)
        (root / 'assets/textures/red.png').write_bytes(png())
        (root / 'assets/models/demo/triangle.obj').write_text('mtllib triangle.mtl\nv -16 0 0\nv 16 0 0\nv 0 32 0\nusemtl red\nf 1 2 3\n')
        (root / 'assets/models/demo/triangle.mtl').write_text('newmtl red\nmap_Kd ../../textures/red.png\n')
        start = {'type': 'PlayerStart', 'pos': [0, 100, 0], 'properties': {'angle': 0}}
        floor = {'id': 'floor', 'pos': [0, -16, 0], 'size': [1024, 32, 1024], 'textures': {'top': 'red.png'}}
        prop = {'type': 'Prop', 'pos': [0, 0, -128], 'properties': {'render_mode': 'model', 'model_path': 'assets/models/demo/triangle.obj', 'solid': False}}
        changer = {'type': 'LevelChanger', 'pos': [100, 64, 0], 'properties': {'target_map': 'sub/second.json'}}
        data = {'version': 3, 'brushes': [floor], 'things': [start, prop, changer]}
        (root / 'maps/start.json').write_text(json.dumps(data))
        data['things'] = [start]
        (root / 'maps/sub/second.json').write_text(json.dumps(data))
        exporter = PackageExporter(None, str(root))
        ok, errors = exporter.export(str(Path(output).absolute()), {'title': 'Universal package contract'}, str(root / 'maps/start.json'))
        if not ok or errors:
            raise RuntimeError(errors)
    return str(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    args = parser.parse_args(argv)
    make_fixture(args.output)


if __name__ == '__main__':
    main()
