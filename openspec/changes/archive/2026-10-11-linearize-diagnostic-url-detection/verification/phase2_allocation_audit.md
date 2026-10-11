# P2 allocation and complexity audit

Change: `linearize-diagnostic-url-detection`; task 2.10.

Run: `PYTHONPATH="$(pwd)" .venv/bin/python openspec/changes/linearize-diagnostic-url-detection/verification/phase2_allocation_audit.py`.
Complete findings and actual exit status: `phase2_allocation_audit.log`.
Result: **P2 ALLOCATION AUDIT PASS**, exit 0.

## Current structures and bounds

- Imports are restricted to `__future__`, `collections`, `dataclasses`, the P1A core and recognition contract. No regex fallback, heap import, legacy detector helper or encoded-variant table exists.
- Scheme trie registration is bounded by the fixed schemes (39 nodes, bound 87). Registered non-empty secrets share immutable P1A compilation; their storage depends on registration.
- Layer provenance and escaped-atom flags retain at most `_MAX_SCHEME_LEN + 4` records. Atom buffers and percent-transducer pending buffers retain fewer than three characters; separator queues retain at most three atoms. There is no duplicate raw tail, decoded search payload or input-sized provenance map.
- Secret provenance windows depend on the longest registered pattern, not stream length. Ordinary encoded text promptly discards decoded outcomes that cannot be covered by a supported URL. The audit checks 5,000 ordinary encoded occurrences, in-URL coverage and exact raw-terminator retention.
- Outcome coordinates are output-sensitive, not payload-free cursor state: pending URL and secret outcomes remain until consumption. This P2 API is not itself a projector pending-budget implementation; P3 owns that integration.

## Registration-bounded secret maintenance

The former start heap and per-token sort were still O(M log M) for a single long token. Their earlier tiny-batch linearity evidence did not establish the required bound and is superseded.

Now each observer owns one speculative deque per registered pattern. Fixed decoded pattern length and advancing emission offsets imply monotonically increasing raw starts and ends within that bucket, even with mixed encoded/literal provenance. Pruning removes obsolete heads; retirement removes completed heads, leaving crossing matches in place. Finalization merges registration-many heads into the globally end-ordered finalized deque. It does not heapify, sort, or inspect historical finalized records.

At P registered patterns, N source units, M emitted records and C commits, maintenance is O(P * (N + M + C)); it is linear at fixed registration. Pattern-bucket count is registration-bounded. Occupancy is updated on insertion/removal without scanning queues. The audit checks ordered buckets and occupancy after each character, absence of heap operations and absence of `sorted` in `_SecretObserver`.

Counters include bucket inspections, record head checks, insertions/removals (`speculative_record_visits`), promoted records (`secret_record_transfers`), and merge-head inspections plus merge dequeue/enqueue operations (`secret_ordering_work`). Counters never decrease on consumption; consumption adds its actual retirement work. No input-sized operation is counted as a constant.

## Deterministic evidence

| Probe | Current result |
|---|---|
| Long URL path pruning, 100/200 `a` units | 1039 -> 2039 visits |
| One long encoded URL, secret `a`, 100/200 `%61` units, including finalization | 3336 -> 6636 total maintenance |
| Same with overlapping `a`, `aa`, `aaa` | 11718 -> 23418 total maintenance |
| 100/200 separate `https://host/%61` tokens | 1500 -> 3000 transfer + ordering work |
| Successive candidate retirement, 50/100 candidates | 200 -> 400 candidate records, each retired once |
| Four-pass encoded scheme prefix `%25252568` | NO_MATCH |
| Shared token boundaries | Raw characters scanned once; `boundary_chars <= len(input) + 8` |
| Decoded coverage | Merged intervals and monotonic sweep, exact once-only coverage |

`CappedPrefixAndLongTokenTests` covers literal and encoded long tokens, overlapping patterns, raw termination and EOF, one-character feeds, commits crossing matches, exact spans, and doubled-input total maintenance. Existing finalization, successive-consumption, secret-retention, differential and provenance regressions remain passing.

## Pending-transducer speculation frontier

Pruning also accounts for the earliest raw start of a pending transducer
unit. A deeper view may still establish a supported prefix even after a
shallower view has emitted secret coverage. Fixed encoding depth and pending
unit bounds constrain this extra speculation window. EOF flush is followed
by pruning before final promotion.

The audit verifies `%%25368ttps://host` starts at 0 and preserves depth-1 secret
[1,4) from `%25`, alongside both literal percent spans. Batch/every-split
regressions verify complete coverage; character-feed/commit probes assert
bounded decoder/speculation state and linear cumulative work.

## Delayed emissions across commits

Secret observers retain a monotonic commit watermark and filter newly emitted
coverage by raw end. Atoms ending within committed source still advance the
cursor and bounded provenance, so longer secret continuations can straddle the
cut. Already committed complete spans are never re-enqueued, including when
pending percent units flush during EOF. This adds constant state/work and no
replay. The audit checks both `zz` continuation and finalization after consuming
`https://host/%`; `CommittedDecoderEmissionTests` additionally verifies exact
cut boundaries and literal/encoded straddlers.

## Scope and residual costs

The scheme cap applies both to completed matches and tentative prefixes: extra decoded atoms cannot start/extend schemes, but can complete the separator after a permitted scheme. A later independently valid supported prefix retains its exact raw start.
Pending escapes at the capped layer can retain only permitted separator edges,
with compatible hex nibbles (`3` for colon, `2` for slash). `%2525256` and
`h%2525256` are NO_MATCH; `https%2525253` retains start 0. The audit and
incomplete-prefix every-split tests verify this cap without dropping positive
depth-four separator retention.

Candidate boundary reuse and coverage sweeps remain unchanged. Batch result selection/deduplication may sort coordinate outputs; this audit does not claim whole-pipeline linearity. It specifically establishes bounded streaming transitions and registration-bounded secret maintenance across feed/finalization/consumption, without payload replay. No P3–P6 implementation or archive claim is made.
