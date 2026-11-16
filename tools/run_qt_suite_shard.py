#!/usr/bin/env python3
"""Run a marked pytest suite as isolated test-module processes across shards.

The Windows Qt suite shares a QApplication and several native Qt objects when
run as one process. A native Qt failure can terminate that process before the
remaining tests execute. This runner keeps every selected test, balances whole
test modules across independent CI jobs, and runs each module in a fresh
Python process so one module cannot corrupt or terminate another.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

# Opt-in tiers (benchmark, soak) are deselected rather than collected and
# skipped: each selected module costs this runner a whole interpreter start.
DEFAULT_MARKER = "qt and not gl and not benchmark and not soak"

#: Wall-clock limit for one module's pytest process. The slowest module on
#: the Windows ARM64 runners takes ~23 min; a hung test otherwise holds the
#: whole shard until the job timeout and every later module goes unreported.
MODULE_TIMEOUT_SECONDS = 30 * 60

#: A single test running this long gets every thread's stack dumped (pytest's
#: faulthandler_timeout; it does not stop the test), so a hang names its
#: cause. The slowest single tests take well under a minute.
TEST_STACK_DUMP_SECONDS = 5 * 60


def run_module(command, cwd, timeout=MODULE_TIMEOUT_SECONDS):
    """Run one module's pytest process; ``(returncode, timed_out)``.

    On timeout the process is killed and the module counts as failed.
    """
    try:
        completed = subprocess.run(command, cwd=cwd, env=os.environ.copy(),
                                   check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, True
    return completed.returncode, False


class _CollectedModules:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.counts: dict[str, int] = {}

    def pytest_collection_finish(self, session) -> None:
        for item in session.items:
            path = Path(str(item.path)).resolve()
            relative = path.relative_to(self.root).as_posix()
            self.counts[relative] = self.counts.get(relative, 0) + 1


def collect_modules(root: Path, marker: str) -> dict[str, int]:
    import pytest

    collector = _CollectedModules(root)
    result = pytest.main(
        ["--collect-only", "-q", "-m", marker],
        plugins=[collector],
    )
    if int(result) != 0:
        raise RuntimeError(f"pytest collection failed with exit code {int(result)}")
    if not collector.counts:
        raise RuntimeError(f"no tests collected for marker: {marker}")
    return collector.counts


def partition_modules(
    counts: dict[str, int], shard_count: int
) -> list[list[tuple[str, int]]]:
    """Greedily balance whole modules by collected test count."""
    shards: list[list[tuple[str, int]]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    for module, count in ordered:
        target = min(range(shard_count), key=lambda index: (loads[index], index))
        shards[target].append((module, count))
        loads[target] += count
    for shard in shards:
        shard.sort(key=lambda pair: pair[0])
    return shards


def junit_test_count(path: Path) -> int:
    root = ET.parse(path).getroot()
    if root.get("tests") is not None:
        return int(root.get("tests", "0"))
    return sum(int(suite.get("tests", "0")) for suite in root.findall(".//testsuite"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--marker", default=DEFAULT_MARKER)
    args = parser.parse_args()

    if args.shard_count < 1:
        parser.error("--shard-count must be at least 1")
    if not 0 <= args.shard_index < args.shard_count:
        parser.error("--shard-index must be between 0 and --shard-count - 1")

    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        counts = collect_modules(root, args.marker)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    shards = partition_modules(counts, args.shard_count)
    selected = shards[args.shard_index]
    expected_total = sum(count for _, count in selected)
    all_total = sum(counts.values())
    print(
        f"Collected {all_total} tests in {len(counts)} modules; "
        f"shard {args.shard_index + 1}/{args.shard_count}: "
        f"{expected_total} tests in {len(selected)} modules.",
        flush=True,
    )

    if not selected:
        print("This shard has no assigned test modules.", flush=True)
        return 0

    failed_modules: list[str] = []
    executed_total = 0

    with tempfile.TemporaryDirectory(prefix="fio-qt-shard-") as temp_dir:
        temp_root = Path(temp_dir)
        for module_index, (module, expected_count) in enumerate(selected, start=1):
            report = temp_root / f"module-{module_index:04d}.xml"
            command = [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-m",
                args.marker,
                "-o",
                f"faulthandler_timeout={TEST_STACK_DUMP_SECONDS}",
                "--junitxml",
                str(report),
                module,
            ]
            print(
                f"\n[{module_index}/{len(selected)}] {module} "
                f"({expected_count} tests)",
                flush=True,
            )
            returncode, timed_out = run_module(command, root)
            if timed_out:
                print(
                    f"ERROR: {module} was still running after "
                    f"{MODULE_TIMEOUT_SECONDS // 60} min and was stopped; the "
                    f"stack dump above shows where it was.",
                    flush=True,
                )

            observed_count = 0
            if report.is_file():
                try:
                    observed_count = junit_test_count(report)
                except (ET.ParseError, OSError, ValueError) as exc:
                    print(f"ERROR: could not read JUnit report: {exc}", flush=True)
            else:
                print("ERROR: pytest did not produce a JUnit report.", flush=True)

            executed_total += observed_count
            if observed_count != expected_count:
                print(
                    f"ERROR: expected {expected_count} tests, "
                    f"JUnit reports {observed_count}.",
                    flush=True,
                )
                failed_modules.append(module)
            if timed_out:
                if module not in failed_modules:
                    failed_modules.append(module)
            elif returncode != 0:
                print(
                    f"ERROR: pytest process exited with code {returncode}.",
                    flush=True,
                )
                if module not in failed_modules:
                    failed_modules.append(module)

    print(
        f"\nShard summary: {executed_total}/{expected_total} tests reported; "
        f"{len(failed_modules)} module(s) failed.",
        flush=True,
    )
    if failed_modules:
        print("Failed modules:", flush=True)
        for module in failed_modules:
            print(f"  - {module}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
