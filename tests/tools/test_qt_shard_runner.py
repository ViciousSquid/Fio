"""tools/run_qt_suite_shard.py: one module cannot hold a whole shard hostage.

A Windows ARM64 shard once hung in a single test until the 90-min job limit,
and the 25 modules after it never ran. The runner now stops a module's
process after MODULE_TIMEOUT_SECONDS, counts it as failed and carries on.
"""

import sys
import time

from tools.run_qt_suite_shard import (MODULE_TIMEOUT_SECONDS, TEST_STACK_DUMP_SECONDS,
                                      partition_modules, run_module)


def test_a_module_that_finishes_reports_its_exit_code(tmp_path):
    assert run_module([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == (3, False)
    assert run_module([sys.executable, "-c", "pass"], tmp_path) == (0, False)


def test_a_module_that_hangs_is_stopped_and_reported(tmp_path):
    started = time.monotonic()
    result = run_module([sys.executable, "-c", "import time; time.sleep(60)"],
                        tmp_path, timeout=1)
    assert result == (None, True)
    assert time.monotonic() - started < 30


def test_stacks_are_dumped_well_before_a_module_is_stopped():
    assert 0 < TEST_STACK_DUMP_SECONDS < MODULE_TIMEOUT_SECONDS / 2


def test_partition_keeps_every_module_exactly_once():
    counts = {"m%02d.py" % i: (i * 7) % 23 + 1 for i in range(40)}
    shards = partition_modules(counts, 6)
    placed = [module for shard in shards for module, _ in shard]
    assert sorted(placed) == sorted(counts)
