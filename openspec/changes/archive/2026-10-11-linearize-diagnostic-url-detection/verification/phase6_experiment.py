"""P6 one-off timing/profile/allocation experiment, outside tests and CI.

Usage: python phase6_experiment.py ROOT before|after OUTPUT.json [repeats]
ROOT is an isolated pre-change export or the current checkout. No timing asserts.
Inputs/sizes/profile sample match phase1_baseline.py. Chunked projector runs use
1024-character chunks on both versions; current recognizer also has one-char
continuation measurements. No historical regex fallback is wired into production.
"""
from pathlib import Path
import cProfile
import dataclasses
import gc
import io
import json
import os
import platform
import pstats
import statistics
import sys
import time
import tracemalloc

ROOT = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2]
OUTPUT = Path(sys.argv[3]).resolve()
REPEATS = int(sys.argv[4]) if len(sys.argv) > 4 else 5
sys.path.insert(0, str(ROOT))

from docker.versioning.diagnostic_projection import DiagnosticProjector, PENDING_LIMIT_BYTES
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_diagnostic_stream import project_tail

if LABEL == 'before':
    from docker.versioning.diagnostic_projection import _find_url_start
else:
    from docker.versioning.diagnostic_recognizer import IncrementalRecognizer

SIZES = (1024, 2048, 4096, 8192, 16384)
PROFILE_SAMPLE = (
    'GET https://registry.example.com/pkg/-/pkg-1.0.0.tgz 15ms done '
    'token=supersecret '
    'custom+transport://user:password@internal.example/private?token=value '
)


def median(fn):
    samples = []
    for _ in range(REPEATS):
        gc.collect()
        start = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - start)
    return {'median_seconds': statistics.median(samples), 'samples_seconds': samples}


def detection(text, chunk):
    if LABEL == 'before':
        return _find_url_start(text)
    r = IncrementalRecognizer()
    for start in range(0, len(text), chunk):
        r.feed(text[start:start + chunk])
    r.finalize()
    return r.earliest_match()


def projection(text, chunk):
    p = DiagnosticProjector()
    out = []
    peak = 0
    for start in range(0, len(text), chunk):
        out.extend(p.feed_text(text[start:start + chunk]))
        peak = max(peak, p.pending_size)
    out.extend(p.finish())
    return {'rendered_length': len(''.join(out)), 'peak_pending_bytes': peak,
            'work': dataclasses.asdict(p.work_stats) if LABEL == 'after' else None}


def allocation(fn):
    gc.collect()
    tracemalloc.start()
    fn()
    current, peak = tracemalloc.get_traced_memory()
    snapshot = tracemalloc.take_snapshot()
    top = [str(item) for item in snapshot.statistics('lineno')[:10]]
    tracemalloc.stop()
    return {'remaining_bytes': current, 'peak_bytes': peak, 'remaining_top_lines': top}


def main():
    data = {'label': LABEL, 'root': str(ROOT), 'command': sys.argv,
            'environment': {'python': platform.python_version(), 'platform': platform.platform(),
                            'executable': sys.executable, 'cpu_count': os.cpu_count(),
                            'pending_limit': PENDING_LIMIT_BYTES, 'repeats': REPEATS},
            'detection': {}, 'projection': {}, 'profiles': {}, 'allocations': {}}
    for size in SIZES:
        token = 'a' * 8
        separated = ' '.join([token] * max(1, size // 9))[:size]
        for shape, text in [('uninterrupted', 'a' * size), ('separated', separated)]:
            chunks = (len(text),) if LABEL == 'before' else (len(text), 1024, 1)
            for chunk in dict.fromkeys(chunks):
                key = f'{size}:{shape}:chunk={chunk}'
                print('detection', key, flush=True)
                data['detection'][key] = {'input_chars': len(text),
                    **median(lambda text=text, chunk=chunk: detection(text, chunk))}
        for chunk in dict.fromkeys((size + 4, 1024)):
            text = 'a' * size + ' end'
            key = f'{size}:chunk={chunk}'
            print('projection', key, flush=True)
            data['projection'][key] = {**projection(text, chunk),
                **median(lambda text=text, chunk=chunk: projection(text, chunk))}
    for mode in NetworkUrlDisplay:
        fn = lambda mode=mode: project_tail(PROFILE_SAMPLE, secrets=('supersecret',), network_url_display=mode)
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(200):
            fn()
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats('cumulative').print_stats(25)
        data['profiles'][mode.value] = {'rendered': fn(), 'repeats': 200,
            'median': median(fn), 'profile': stream.getvalue()}
        data['allocations'][mode.value] = allocation(fn)
    # Input-doubling allocations are descriptive; live-state bounds are proven
    # independently by deterministic all-instance audits, not peak allocator noise.
    for size in (8192, 16384):
        data['allocations'][f'projection:{size}'] = allocation(lambda size=size: projection('a' * size + ' end', 1024))
    OUTPUT.write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(data, indent=2))
    print('experiment complete; no elapsed-time assertions')


if __name__ == '__main__':
    main()
