"""Compare two ``tools/oracle_capture.py`` captures, bit for bit.

    python tools/oracle_diff.py before.npz after.npz [--noise again.npz]

Prints how many records are identical and, per field, where they differ
(pixels: how many and their bounding box). With ``--noise`` (a second capture
of the *after* revision), an element that differs before/after is only
reported when it is stable between the two after-runs -- what remains is a
difference the change made, not one the run made. Exit status 1 when
anything remains.
"""
from __future__ import annotations

import argparse
import collections
import sys


def main(argv=None):
    import numpy as np
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--noise")
    args = parser.parse_args(argv)
    a, b = np.load(args.before), np.load(args.after)
    noise = np.load(args.noise) if args.noise else None
    only = set(a.files) ^ set(b.files)
    diffs = collections.defaultdict(list)
    same = 0
    for key in sorted(set(a.files) & set(b.files)):
        x, y = a[key], b[key]
        if x.shape == y.shape and x.dtype == y.dtype and x.tobytes() == y.tobytes():
            same += 1
            continue
        if noise is not None and key in noise.files and x.shape == y.shape == noise[key].shape:
            changed = x != y
            unstable = y != noise[key]
            if not (changed & ~unstable).any():
                same += 1
                continue
        source, level, tick, *field = key.split("/")
        field = "/".join(field)
        if field == "pixels" and x.shape == y.shape:
            hit = np.any(x != y, axis=2)
            ys, xs = np.nonzero(hit)
            diffs[(source, field)].append((level, tick, "%d px in y%d-%d x%d-%d" % (
                hit.sum(), ys.min(), ys.max(), xs.min(), xs.max())))
        else:
            count = int(np.sum(x != y)) if x.shape == y.shape else None
            diffs[(source, field)].append((level, tick, x.shape, y.shape, count))
    print("identical records: %d   differing: %d   in one capture only: %d" % (
        same, sum(len(v) for v in diffs.values()), len(only)))
    for key in sorted(only)[:20]:
        print("  only in one:", key)
    for (source, field), where in sorted(diffs.items()):
        print("  %-6s %-40s %d checkpoints, e.g. %s" % (source, field, len(where), where[0]))
    return 1 if diffs or only else 0


if __name__ == "__main__":
    sys.exit(main())
