"""Opt-in ZIP preload benchmark, excluding audio decoding and device startup.

python -m pytest tests/performance/test_audio_preload_benchmark.py \
    --run-benchmarks -s -q
"""

import gc
from pathlib import Path
import random
import time
import zipfile

import pytest

from engine.audio_archives import AudioArchiveReader
from tests.helpers import audio_archive_reference as reference

pytestmark = [pytest.mark.benchmark, pytest.mark.slow]


def _old(path):
    return [(member, reference.read_archive_audio(path, member, 'assets/sounds'))
            for member in reference.list_archive_audio(path, 'assets/sounds')]


def _new(path):
    with AudioArchiveReader(path, 'assets/sounds') as reader:
        return [(member, reader.read(member)) for member in reader.members()]


def _report(path, label):
    expected = _old(path)
    assert _new(path) == expected
    count = len(expected)
    size = sum(len(data) for _, data in expected)
    del expected
    samples = [[], []]
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        for repeat in range(7):
            for index in ((0, 1) if repeat % 2 else (1, 0)):
                start = time.perf_counter_ns()
                result = (_old, _new)[index](path)
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                assert len(result) == count
                del result
                samples[index].append(elapsed)
    finally:
        if enabled:
            gc.enable()
    before, after = (min(values) for values in samples)
    print(f'\nAUDIO_PRELOAD {label} {count} {size} '
          f'{before:.3f} {after:.3f} {before / after:.2f}x', flush=True)


@pytest.mark.parametrize('count', [1, 16, 128, 1024])
@pytest.mark.parametrize('compression', [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_synthetic_archive_preload(tmp_path, count, compression):
    path = tmp_path / 'sounds.fiopak'
    rng = random.Random(192)
    # Alternating compressible and incompressible 4 KiB payloads, nested names.
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for index in range(count):
            data = b'wave' * 1024 if index % 2 else rng.randbytes(4096)
            archive.writestr(f'assets/sounds/group{index % 4}/{index:04}.wav', data)
    _report(path, f'synthetic-{compression}')


def test_bundled_archive_preload():
    path = Path(__file__).resolve().parents[2] / 'assets/sounds.fiopak'
    if not path.is_file():
        pytest.skip('bundled sounds.fiopak absent')
    _report(path, 'bundled')
