"""Verify a universal .fiopak using the same readers and simulation as play."""
import argparse

from player.runtime.fiopak import FioPackage, PackageError
from player.runtime.package_assets import map_assets, companion_assets


def verify_package(path, *, simulate=True):
    maps, assets = 0, set()
    with FioPackage.open(path) as package:
        package.start_map_path()
        for map_path in package.list_maps():
            data = package.load_map(map_path)
            pending = list(map_assets(data))
            while pending:
                reference = pending.pop()
                resolved = package._resolve(reference.replace('\\', '/'))
                if resolved is None:
                    raise PackageError(f'{map_path}: missing asset {reference}')
                if resolved in assets:
                    continue
                raw = package.read_asset(resolved)
                if raw is None:
                    raise PackageError(f'{map_path}: unreadable or oversized asset {reference}')
                assets.add(resolved)
                pending.extend(companion_assets(resolved, raw))
            if simulate:
                from player.world import PlayerWorld
                from player.input.state import InputState
                world = PlayerWorld(data, package)
                try:
                    world.step(1/30, InputState(), 90 - __import__('math').degrees(world.player.angle), 0)
                finally:
                    world.close()
            maps += 1
    return maps, len(assets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package')
    args = parser.parse_args(argv)
    try:
        maps, assets = verify_package(args.package)
    except Exception as exc:
        print(f'FAIL: {exc}')
        return 1
    print(f'PASS: {maps} maps, {assets} referenced assets, shared engine simulation')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
