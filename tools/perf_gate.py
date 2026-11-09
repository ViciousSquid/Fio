"""Interleaved performance gate: one checkout against another.

    python tools/perf_gate.py --base PATH --head PATH [--rounds 5] [--core 2]
        [--probes movers,aabb,static,ai,player,table,frames,gameplay] [--json out.json]

Runs every probe of ``tools/perf_probes.py`` -- the same measuring code for
both checkouts -- in a fresh process pinned to one core, alternating base and
head (the order flips each round so drift and warm-up fall on both). The
``frames`` and ``gameplay`` probes need a display (``xvfb-run -a``;
``LIBGL_ALWAYS_SOFTWARE=1``).

For each metric it reports, per checkout, p50 / p95 / p99 over all samples of
all rounds, the head/base ratio of the p50s, and a one-sided Mann-Whitney U
test of "head slower than base" on the per-round medians (rounds are the
independent unit; samples within a run are not). A metric is a
**regression** when the head is slower with p < 0.05 and by more than the
noise floor (``--floor``, default 2 %): no statistically meaningful
regression is the gate.
"""

import argparse
import itertools
import json
import os
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = ('movers', 'aabb', 'static', 'ai', 'player', 'table', 'frames', 'gameplay')


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return float('nan')
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def mann_whitney_greater(xs, ys):
    """One-sided exact Mann-Whitney U p-value for "xs tend to exceed ys"
    (small samples: exact permutation distribution of U)."""
    n, m = len(xs), len(ys)
    if not n or not m:
        return 1.0

    def u_stat(a, b):
        u = 0.0
        for x in a:
            for y in b:
                u += 1.0 if x > y else (0.5 if x == y else 0.0)
        return u

    observed = u_stat(xs, ys)
    pooled = list(xs) + list(ys)
    count = total = 0
    for chosen in itertools.combinations(range(n + m), n):
        picked = set(chosen)
        a = [pooled[i] for i in chosen]
        b = [pooled[i] for i in range(n + m) if i not in picked]
        total += 1
        if u_stat(a, b) >= observed - 1e-12:
            count += 1
    return count / total


def run_probe(probe, tree, core):
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as handle:
        path = handle.name
    command = [sys.executable, os.path.join(HERE, 'perf_probes.py'), probe,
               '--tree', tree, '--out', path]
    if core is not None:
        command = ['taskset', '-c', str(core)] + command
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=3600)
    with open(path) as handle:
        data = json.load(handle)
    os.unlink(path)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--core', type=int, default=2)
    parser.add_argument('--probes', default=','.join(PROBES))
    parser.add_argument('--floor', type=float, default=0.02)
    parser.add_argument('--json')
    args = parser.parse_args()
    trees = {'base': os.path.abspath(args.base), 'head': os.path.abspath(args.head)}
    probes = [p for p in args.probes.split(',') if p]
    samples = {'base': {}, 'head': {}}       # metric -> list of per-round lists
    for round_index in range(args.rounds):
        order = ('base', 'head') if round_index % 2 == 0 else ('head', 'base')
        for probe in probes:
            for side in order:
                data = run_probe(probe, trees[side], args.core)
                for metric, values in data.items():
                    samples[side].setdefault(metric, []).append(values)
        print('round %d/%d done' % (round_index + 1, args.rounds), file=sys.stderr)
    report = {}
    regressions = []
    for metric in sorted(samples['base']):
        if metric not in samples['head']:
            continue
        row = {}
        for side in ('base', 'head'):
            pooled = [v for run in samples[side][metric] for v in run]
            row[side] = {'p50': percentile(pooled, 0.5), 'p95': percentile(pooled, 0.95),
                         'p99': percentile(pooled, 0.99),
                         'round_medians': [statistics.median(run) for run in samples[side][metric]]}
        ratio = row['head']['p50'] / row['base']['p50'] if row['base']['p50'] else float('nan')
        p = mann_whitney_greater(row['head']['round_medians'], row['base']['round_medians'])
        verdict = 'regression' if (p < 0.05 and ratio > 1.0 + args.floor) else (
            'faster' if mann_whitney_greater(row['base']['round_medians'],
                                             row['head']['round_medians']) < 0.05
            and ratio < 1.0 - args.floor else 'same')
        row.update(ratio=ratio, p=p, verdict=verdict)
        report[metric] = row
        if verdict == 'regression':
            regressions.append(metric)
    width = max(len(m) for m in report) if report else 10
    print('%-*s %28s %28s %7s %6s  %s' % (width, 'metric', 'base p50/p95/p99',
                                         'head p50/p95/p99', 'ratio', 'p', 'verdict'))
    for metric, row in report.items():
        b, h = row['base'], row['head']
        print('%-*s %9.3f %8.3f %9.3f %9.3f %8.3f %9.3f %7.3f %6.3f  %s' % (
            width, metric, b['p50'], b['p95'], b['p99'], h['p50'], h['p95'], h['p99'],
            row['ratio'], row['p'], row['verdict']))
    print('\n%d metrics, %d regressions%s' % (
        len(report), len(regressions), (': ' + ', '.join(regressions)) if regressions else ''))
    if args.json:
        with open(args.json, 'w') as handle:
            json.dump({'trees': trees, 'rounds': args.rounds, 'floor': args.floor,
                       'report': report}, handle, indent=1)
    return 1 if regressions else 0


if __name__ == '__main__':
    sys.exit(main())
