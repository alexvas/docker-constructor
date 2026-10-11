"""Render P6 measured comparisons from preserved experiment outputs."""
from pathlib import Path
import json

HERE = Path(__file__).resolve().parent


def main():
    before = json.loads((HERE / 'phase6_before.json').read_text())
    after = json.loads((HERE / 'phase6_after.json').read_text())
    lines = [
        '# P6 — one-off before/after performance and allocation evidence (6.3)', '',
        'Experiments run outside tests/CI using the same project CPython 3.14.7.',
        'Before: isolated `git archive 50f9ebc` export (pre-implementation). After:',
        'current production checkout. `phase6_experiment_environment.log` records',
        'the full revision/export location; JSON records commands/platform/CPU/repeats.',
        'No working-tree reset, staging or baseline production fallback was used.', '',
        'Five samples per median; timing runs are unprofiled/untraced and call GC',
        'before each repeat. Raw samples and full output/statuses are preserved in',
        '`phase6_before.json`, `phase6_after.json`, and their `*_experiment.log` files.',
        'Inputs are the historical 1/2/4/8/16 KiB runs, separated eight-character',
        'tokens, `a * N + " end"`, and the unchanged phase1 profile sample. Profiles',
        'are 200 calls of `project_tail` per mode, with 25 cumulative entries.', '',
        '## Isolated uninterrupted recognition', '',
        'Before times the old `_find_url_start`; after times a new production',
        '`IncrementalRecognizer` including construction, feed and finalization.',
        'This is not an identical API benchmark: the new call does more work.',
        'Historical `phase1_baseline.log` is corroborated by a fresh before run.',
        'The old detector has no continuation API, so no invented old chunked',
        'detector is compared; the common projector API below covers chunked before/after.', '',
        '| KiB | Before single feed (s) | After single feed (s) | After 1024-char feeds (s) | After one-char feeds (s) |',
        '| --- | --- | --- | --- | --- |',
    ]
    for size in (1024, 2048, 4096, 8192, 16384):
        key = f'{size}:uninterrupted:chunk={size}'
        values = [before['detection'][key]['median_seconds'], after['detection'][key]['median_seconds'],
                  after['detection'][f'{size}:uninterrupted:chunk=1024']['median_seconds'],
                  after['detection'][f'{size}:uninterrupted:chunk=1']['median_seconds']]
        lines.append(f'| {size//1024} | ' + ' | '.join(f'{v:.6f}' for v in values) + ' |')
    lines += ['', '## Same projector API, unchanged synthetic input', '',
              '| KiB | Before single (s) | After single (s) | Before 1024-char chunks (s) | After 1024-char chunks (s) |',
              '| --- | --- | --- | --- | --- |']
    for size in (1024, 2048, 4096, 8192, 16384):
        values = [data['projection'][f'{size}:chunk={chunk}']['median_seconds']
                  for chunk in (size + 4, 1024) for data in (before, after)]
        lines.append(f'| {size//1024} | ' + ' | '.join(f'{v:.6f}' for v in values) + ' |')
    lines += ['', 'The old arbitrary-scheme pending token overflows at 8 KiB, so its',
              '8/16 KiB projector timings plateau; the supported-only implementation',
              'treats this `a` run as ordinary text. Rendering differences are intentional,',
              'not a reason to compare old and new marker output as equivalent.', '',
              '## Capture and allocator trade-offs (not all workloads improve)', '',
              '| Mode | Before median capture (s) | After median capture (s) | Before traced peak bytes | After traced peak bytes |',
              '| --- | --- | --- | --- | --- |']
    for mode in ('redacted', 'host-path', 'exact'):
        b, a = before['profiles'][mode], after['profiles'][mode]
        lines.append(f"| {mode} | {b['median']['median_seconds']:.6f} | {a['median']['median_seconds']:.6f} | {before['allocations'][mode]['peak_bytes']} | {after['allocations'][mode]['peak_bytes']} |")
    lines += ['', 'Safe-mode short capture is slower and has higher transient allocation:',
              'the persistent fixed-depth Python recognizer and compiled secret registration',
              'do more work than the old short-string regex path. Exact capture is close.',
              'Separated-run medians, complete profile call stacks and traced remaining',
              'allocation sites are included in the JSON; these regressions are not hidden.',
              'No absolute throughput/allocation acceptance threshold was specified.',
              'This change proves removal of quadratic candidate search, not a speedup',
              'for every input, lower total allocations or whole-pipeline linearity.', '',
              'Tracemalloc samples are separate from timing runs, with remaining and peak',
              'bytes/top retained sites. Input-doubling projection allocator samples are',
              'also recorded. These are descriptive allocator data, not bounded-live-state',
              'proof; the recursive state/counter audits supply that independent evidence.', '',
              '## Unchanged historical oversized/retained-tail fixtures', '',
              'The exact `phase1_oversized_fixture_baseline.py` is used for both versions,',
              'five repeats for all fourteen scenarios. No fixtures, sizes, chunking or',
              'stress counts are reduced. Full observable outcomes and raw timings are in',
              '`phase6_before_fixtures.log` / `phase6_after_fixtures.log` (both exit 0).', '',
              '| Fixture | Before median (s) | After median (s) |', '| --- | --- | --- |']
    fixture_results = []
    for label in ('before', 'after'):
        text = (HERE / f'phase6_{label}_fixtures.log').read_text().split('--- medians ---')[1].lstrip()
        fixture_results.append(json.JSONDecoder().raw_decode(text)[0])
    assert len(fixture_results[0]) == len(fixture_results[1]) == 14
    for b, a in zip(*fixture_results):
        assert b['test_id'] == a['test_id']
        lines.append(f"| {a['label']} | {b['median_seconds']:.6f} | {a['median_seconds']:.6f} |")
    lines += ['', 'The two original retained-tail bottlenecks improve from about 7.8 s to',
              '2.6 s with their `TAIL_BYTES * 2` uninterrupted payloads unchanged. Some',
              'already-fast URL and ordinary line cases become slower. No optimization',
              'of fetch long-authority parsing or unrelated rendering is claimed.', '',
              'The historical `_DrainEntryProbe._drain` hook no longer runs in the',
              'coordinated projector; its new-version zero observation is **not** used',
              'as a bound proof. Current tests use `_drain_coordinated` and the separate',
              '`phase6_ownership_work.log` audit checks the whole pending buffer on every',
              'feed. Historical oversized-marker observations likewise are not assertions',
              'of the revised early-commit marker contract.', '',
              '## Deterministic work / ownership reconciliation', '',
              '`phase6_core_audit.log`, `phase6_recognizer_audit.log`, and',
              '`phase6_ownership_work.log` rerun final-state immutable-registration,',
              'bounded provenance/speculation, output-link/maintenance and all-instance',
              'cumulative work/one-raw-owner audits. `phase6_differential.log` reruns',
              '33558 partition comparisons of text and ordered permitted facts. The full',
              'suite additionally covers every phase and current policy/display products.',
              'No fingerprints or presentation-session keys are written into this evidence.', '',
              'The measured isolated scan approximately doubles per input doubling rather',
              'than quadrupling. Deterministic lifetime counters, not elapsed-time tests,',
              'establish replay-free fixed-registration/fixed-depth recognition bounds.',
              'All experiment/audit processes exited 0. No timing assertions were added',
              'to automated tests or CI.', '']
    (HERE / 'phase6_performance.md').write_text('\n'.join(lines))
    print('P6 comparisons rendered: all sizes/modes, 14 unchanged fixtures, regressions disclosed')


if __name__ == '__main__':
    main()
