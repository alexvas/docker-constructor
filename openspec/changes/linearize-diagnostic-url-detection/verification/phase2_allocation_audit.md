=== P2 allocation / fallback audit (task 2.10) ===
target: docker/versioning/diagnostic_recognizer.py

--- patterns that must be absent (regex fallback / variant table / legacy detector) ---
4:unanchored ``_URL_START_RE.search`` hotspot with a linear recognizer that
30:  ``_url_prefix_state_single`` state of the current raw token from its first
452:    """Legacy single-layer ``_url_prefix_state_single`` over one layer's token.
(matches above)

--- module imports ---
89:from __future__ import annotations
91:from collections import namedtuple
92:from dataclasses import dataclass
94:from docker.versioning.diagnostic_recognition import (

--- list/str/allocation sites in the module ---
210:    out: list[str] = []
218:                out.append(chr(int(pair, 16)))
221:        out.append(character)
223:    return "".join(out)
296:        self._pending: list[tuple[str, int, int, bool]] = []
319:        self._pending.append((character, source_start, source_end, from_token))
336:                del pending[:3]
371:        self._buf: list[tuple[str, int, int]] = []
372:        self._queue: list[_Atom] = []
385:        self._buf.append((character, source_start, source_end))
403:                del buf[:3]
412:        queue.append(atom)
465:        self._pend: list[str] = []
472:            self._pend.append(character)

--- wc ---
674 docker/versioning/diagnostic_recognizer.py

--- interpretation (task 2.10) ---

Findings: four bounded-allocation/provenance defects were found and fixed
during P2 (two allocation, one counter, one provenance).

Defect 1 (pre-slice copy). `_recognize` sliced `text[start:colon]` *before*
`_classify_raw_scheme` length-checked it, copying an arbitrarily long scheme
into an input-sized string. Fixed with an explicit pre-slice length guard.

Defect 2 (bound did not cover supported spellings). The initial fixed bound of
256 raw characters was too small. `KNOWN` is decided after up to `_LAYERS` (4)
percent-decoding passes, and each pass can expand every character by 3x, so a
supported spelling of a whitelist scheme can use up to `3 ** _LAYERS` (81) raw
characters per decoded character. The longest whitelist scheme is `git+https`
(9 characters), so a fully four-times-encoded known scheme is up to
`9 * 81 = 729` raw characters; in particular `https` encoded four times is 405
raw characters, which the 256 bound misclassified `UNKNOWN`. The bound is now
derived, not fixed:

```
_CLASSIFY_BOUND = max(len(scheme) for scheme in KNOWN_SCHEMES) * (3 ** _LAYERS)
```

i.e. 729. The pre-slice guard is unchanged, so a longer (unsupported) span is
still never copied and is classified `UNKNOWN` as `MATCH`.

Why the derived bound covers every supported spelling:

* The classifier decodes at most `_LAYERS` passes; a raw span longer than
  `max_scheme_len * 3 ** _LAYERS` cannot decode to a whitelist scheme in that
  many passes (when fewer passes are needed the span is shorter, never longer).
* Percent-encoding a character always yields exactly `%XX` (3 characters), and
  separately encoding the `%` and hex digits of an earlier escape is just
  another encoding pass, so `3 ** _LAYERS` is the exact maximum expansion per
  character (including separately encoded escape characters).
* Case is irrelevant to length and is handled by `classify_scheme`'s
  case-insensitive comparison.

The bound concerns recognition/classification only; it does not change the
detection depth, fetch grammar, projector integration or interpretation policy.

Defect 3 (vacuous work counter). `RecognitionStats.emissions` was never
incremented, so the claimed transducer-work metric was always zero and its
linearity check compared two zeros. `_Transducer._emit` now increments
`stats.emissions` for every character forwarded to a sink: decoded `%XX`,
copied literals, and flushed pending characters. Tests assert non-zero
emissions for representative non-empty inputs and zero for empty input, and the
existing linear-growth assertion now validates real work.

Defect 4 (missing raw range provenance). Emitted characters carried only a raw
source start, but the P2 design requires an inclusive raw start and exclusive
raw end. `_Transducer` now threads `(source_start, source_end)` through
`_Pipeline` into `_MatchObserver`, where each `_Atom` stores both ends. A
decoded `%XX` ranges over all three raw characters (and spans the contributing
ranges when the escape came from an earlier layer), a copied literal spans its
one raw character, and malformed/incomplete escapes preserve the literal
ranges of their characters. Selection still uses the range start; the exclusive
end is retained as `match_start_end` / `match_colon_end` for incremental work
in P3. Returned candidate coordinates and the legacy differential corpus are
unchanged.

* The pattern hits are documentation text that names the legacy behavior being
  replaced (module docstring and the `_PrefixObserver` class docstring). There
  is no `import re`, no `_URL_START_RE` use, and no call to
  `_find_url_start`, `_decode_percent_layer` or `_url_prefix_state`.
* The module imports only `__future__`, `collections.namedtuple`,
  `dataclasses.dataclass`, and the P1 `diagnostic_recognition` contract. It
  does not import the legacy detector, the projector, `re`, or any variant
  table, and the test `test_recognizer_has_no_legacy_regex_or_detector_fallback`
  asserts the absence of those names.
* Allocation sites are bounded: `_Transducer._pending` holds at most three
  characters (now `(character, start, end, from_token)` tuples),
  `_MatchObserver._buf` at most two and `_MatchObserver._queue` at most three
  atoms, and `_PrefixObserver._pend` at most two characters. No input-sized
  decoded search copy, per-layer source map or encoded-variant enumeration is
  constructed.
* Classification copy is bounded by the pre-slice guard in `_recognize`:

  ```python
  if colon - start > _CLASSIFY_BOUND:
      kind = CandidateKind.UNKNOWN
  else:
      kind = _classify_raw_scheme(text[start:colon])
  ```

  Only spans of at most `_CLASSIFY_BOUND` (729 = `9 * 3 ** 4`) characters are
  sliced and passed to `_classify_raw_scheme` / `_decode_one_layer`; longer
  schemes remain detectable `MATCH` candidates classified `UNKNOWN` without any
  scheme-sized copy. The classifier's own length check is retained as a
  defensive bound.
* Deterministic allocation regression coverage:
  `tests/test_diagnostic_recognizer_phase2.py::BoundedClassificationAllocationTests`
  proves with `unittest.mock` that `_classify_raw_scheme` is **not** called for
  a `_CLASSIFY_BOUND * 20` scheme at a nonzero start offset or for a
  `_CLASSIFY_BOUND + 1` scheme, while it **is** called exactly once with a
  `_CLASSIFY_BOUND`-character span when the scheme length equals
  `_CLASSIFY_BOUND`; the long schemes still return `MATCH` with exact raw
  boundaries and `UNKNOWN` kind. No timing is asserted. Evidence:
  `phase2_classification_red.log` (pre-fix: 2 failed, exit 1) and
  `phase2_classification_green.log` (post-fix: 3 passed, exit 0).
* Deterministic classification coverage:
  `…::WhitelistClassificationTests` covers every whitelist scheme at depths
  zero through four with uppercase and lowercase hex, uppercase scheme
  spellings, mixed per-character depths, separately encoded escape characters,
  and the 405-character four-times-encoded `https`; all classify `KNOWN` with
  exact raw boundaries. Evidence: `phase2_whitelist_red.log` (pre-fix bound
  256: 37 failed, exit 1).
* Deterministic work-counter coverage:
  `…::WorkStateRegressionTests` asserts non-zero `emissions` for representative
  inputs, zero for the empty input, and linear growth for the long-scheme runs.
  Evidence: `phase2_emissions_red.log` (pre-fix: 7 failed, exit 1).
* Deterministic range-provenance coverage:
  `…::SourceRangeProvenanceTests` asserts single-character literal ranges,
  three-character decoded `%XX` ranges, literal ranges for malformed
  (`%zz`, `%4G`) and trailing incomplete (`%4`) escapes, nested-decode spans
  that cover all contributing raw characters, observer-retained
  `match_start_end` / `match_colon_end`, and unchanged candidate coordinates.
  Evidence: `phase2_ranges_red.log` (pre-fix: 3 failed, exit 1).
* There is no performance-only scheme limit: arbitrary-length UNKNOWN schemes
  remain candidates (`test_arbitrarily_long_unknown_scheme_is_detected`) and
  classification rejects an unsupported length only as `UNKNOWN`, never by
  dropping the candidate.
* Final P2 state: `phase2_green.log` (64 passed, 4762 subtests, exit 0),
  `phase2_types.log` (exit 0) and `phase2_validation.log` (642 passed, 11904
  subtests, exit 0).
