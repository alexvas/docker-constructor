"""One-off P2 allocation / speculation / fallback / decoding-scope audit.

Task 2.10 of ``linearize-diagnostic-url-detection`` requires proof that the raw
recognizer keeps no duplicate raw tail, no decoded search buffer, no
input-sized search map, no encoded-variant enumeration and no regular
expression fallback, and that its speculative work and registration storage are
bounded by configured patterns rather than by stream length.

This is a one-off, timing-free inspection run outside tests/CI.  It prints each
finding and ``P2 ALLOCATION AUDIT PASS`` on success, exiting 0.  It is not part
of the production import graph and introduces no dependency.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from collections import deque

from docker.versioning import diagnostic_recognizer as recognizer

_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "docker.versioning.aho_corasick",
        "docker.versioning.diagnostic_recognition",
    }
)

_FORBIDDEN_NAMES = (
    "_URL_START_RE",
    "_find_url_start",
    "_decode_percent_layer",
    "_url_prefix_state",
    "_url_prefix_state_single",
)


def _check(condition: bool, message: str) -> None:
    status = "ok" if condition else "FAIL"
    print(f"[{status}] {message}")
    if not condition:
        raise SystemExit(1)


def audit_imports_and_fallback() -> None:
    source = pathlib.Path(recognizer.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    unexpected = sorted(set(imported) - _ALLOWED_IMPORTS)
    _check(not unexpected, f"module imports only the allowlist; unexpected={unexpected}")
    _check(not hasattr(recognizer, "re"), "module does not import the re module")
    _check(
        not hasattr(recognizer, "heapify"),
        "module never heapifies a historical finalized collection",
    )
    for name in _FORBIDDEN_NAMES:
        _check(not hasattr(recognizer, name), f"module does not define {name}")
    variant_names = [name for name in vars(recognizer) if "VARIANT" in name.upper()]
    _check(not variant_names, f"module defines no encoded-variant table: {variant_names}")


def audit_registration_storage() -> None:
    automaton = recognizer._SCHEME_AUTOMATON
    bound = 1 + sum(len(scheme) for scheme in recognizer._SCHEME_PATTERNS)
    _check(
        automaton.node_count <= bound,
        f"scheme automaton node_count {automaton.node_count} <= {bound}",
    )
    secrets = ("TOKEN", "password done", "TOKENht")
    pipeline = recognizer._Pipeline(recognizer._MutableStats(), secrets=secrets)
    secret_patterns = recognizer.AhoCorasick(secrets).patterns
    _check(
        len(secret_patterns) == len(secrets),
        "secret automaton registers every non-empty secret once",
    )


def audit_bounded_state() -> None:
    stats = recognizer._MutableStats()
    pipeline = recognizer._Pipeline(stats, secrets=("TOKEN",))
    text = ("%41" * 20_000) + " " + ("x" * 20_000) + " " + ("%254f" * 10_000)
    pipeline.feed(text)
    for layer in pipeline._layer:
        _check(
            len(layer._raw_starts) <= recognizer._MAX_SCHEME_LEN + 4,
            "layer provenance map stays bounded by the scheme set",
        )
        _check(
            len(layer._escaped_atoms) <= recognizer._MAX_SCHEME_LEN + 4,
            "layer escaped-atom set stays bounded by the scheme set",
        )
        _check(len(layer._buf) < 3, "layer atom buffer stays below three characters")
        _check(len(layer._queue) <= 3, "layer separator queue stays at most three atoms")
    observers = [pipeline._literal, *pipeline._decoded]
    for observer in observers:
        _check(
            len(observer._spans) <= max(observer._max_len, 1),
            "secret provenance window stays bounded by the longest secret",
        )
        _check(
            isinstance(observer._speculative, tuple)
            and len(observer._speculative) == len(observer._cursor._automaton.patterns)
            and all(isinstance(bucket, deque) for bucket in observer._speculative),
            "speculation uses registration-many ordered deques, without a heap",
        )
        _check(
            isinstance(observer._matches, deque),
            "finalized secret matches use an end-ordered deque",
        )
    for transducer in pipeline._transducers:
        _check(
            len(transducer._pending) < 3,
            "transducer pending buffer stays below three characters",
        )


def audit_pruning_and_coverage_work() -> None:
    for suffix in ("", "zz"):
        stream = recognizer.IncrementalRecognizer(("%",))
        stream.feed("https://host/%")
        before = stream.stats.secret_emissions
        stream.consume(14)
        _check(stream.stats.secret_emissions == before,
               "commit keeps decoder/secret state without replay")
        stream.feed(suffix)
        stream.finalize()
        _check(not stream.decoded_secrets(),
               "delayed decoder emissions do not resurrect committed secrets")
    result, secrets = recognizer.recognize_with_secrets("%%25368ttps://host", ("%",))
    _check(result.candidate is not None and result.candidate.start == 0,
           "pending transducer establishes URL start at zero")
    _check({(m.start, m.end, m.depth) for m in secrets}
           == {(0, 1, 0), (1, 2, 0), (1, 4, 1)},
           "pending decoder provenance preserves complete decoded-secret coverage")
    def prune_visits(length: int) -> int:
        pipeline = recognizer._Pipeline(
            recognizer._MutableStats(), incremental=True, secrets=("a",)
        )
        pipeline.feed("https://host/" + "a" * length)
        return pipeline.stats().speculative_record_visits

    small = prune_visits(100)
    large = prune_visits(200)
    _check(small > 0, "decoded-secret pruning does observable work")
    _check(
        large <= 2 * small + 8,
        f"pruning visits grow at most linearly: {small} -> {large}",
    )

    incremental = recognizer.IncrementalRecognizer(("a",))
    incremental.feed("https://host/" + "a" * 50)
    before = incremental.stats.speculative_record_visits
    incremental.consume(incremental.consumed)
    _check(
        incremental.stats.speculative_record_visits >= before,
        "pruning counters survive consumption",
    )

    def coverage_work(count: int):
        text = " ".join(
            f"https://host{index:03d}/%54OKEN" for index in range(count)
        )
        return recognizer.recognize_with_secrets_and_stats(text, ("TOKEN",))

    _result, matches, small_stats = coverage_work(10)
    _result, large_matches, large_stats = coverage_work(20)
    _check(len(matches) == 10, "coverage reports each decoded secret once")
    _check(
        len(large_matches) == 20, "coverage reports scale with the URL count"
    )
    _check(
        large_stats.boundary_chars <= 2 * small_stats.boundary_chars + 16,
        "terminator boundary scanning grows at most linearly",
    )
    _check(
        large_stats.coverage_checks <= 2 * small_stats.coverage_checks + 16,
        "coverage sweep comparisons grow at most linearly",
    )
    embedded = "".join(
        f"https://h{index:03d}/%54OKEN" for index in range(20)
    )
    _embedded_result, embedded_matches, embedded_stats = (
        recognizer.recognize_with_secrets_and_stats(embedded, ("TOKEN",))
    )
    _check(
        len(embedded_matches) == 20,
        "embedded candidates in one token all get coverage",
    )
    _check(
        embedded_stats.boundary_chars <= len(embedded) + 8,
        "shared boundaries scan a single-token input once",
    )


def audit_finalization_and_retirement_work() -> None:
    _check(not hasattr(recognizer, "heappush"), "secret maintenance has no heap operations")
    tree = ast.parse(pathlib.Path(recognizer.__file__).read_text())
    observer_class = next(node for node in tree.body
                          if isinstance(node, ast.ClassDef) and node.name == "_SecretObserver")
    _check(not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "sorted" for node in ast.walk(observer_class)),
           "secret maintenance contains no input-sized sort")
    probe = recognizer.IncrementalRecognizer(("a", "aa", "aaa"))
    for character in "https://host/" + "%61a%2561" * 40:
        probe.feed(character)
        for observer in probe._pipeline._decoded:
            for bucket in observer._speculative:
                records = list(bucket)
                _check(all(a[0] <= b[0] and a[1] <= b[1]
                           for a, b in zip(records, records[1:])),
                       "fixed-pattern emission buckets stay start/end ordered")
            _check(observer._retained == len(observer._matches)
                   + sum(map(len, observer._speculative)),
                   "incremental occupancy equals actual records")
    for text in ("%252525", "%2525256", "h%2525256", "https%2525256"):
        _check(recognizer.recognize(text).kind.name == "NO_MATCH",
               f"incomplete fourth-pass scheme escape is not retained: {text}")
    _check(recognizer.recognize("https%2525253").retain_from == 0,
           "pending fourth-pass separator retains its permitted scheme")
    _check(recognizer.recognize("%25252568").kind.name == "NO_MATCH",
           "four-pass scheme prefix is not retained")
    for patterns in (("a",), ("a", "aa", "aaa")):
        work = []
        for size in (100, 200):
            text = "https://host/" + "%61" * size
            _, matches, stats = recognizer.recognize_with_secrets_and_stats(text, patterns)
            _check(len(matches) == sum(size - len(p) + 1 for p in patterns),
                   "long encoded token preserves every overlapping secret")
            work.append(stats.secret_ordering_work + stats.secret_record_transfers
                        + stats.speculative_record_visits)
        _check(work[1] <= 2 * work[0] + 64,
               f"long-token total maintenance is linear {patterns}: {work}")
    def finalize_work(reps: int) -> tuple[int, int]:
        text = " ".join(["https://host/%61"] * reps)
        result, matches, stats = recognizer.recognize_with_secrets_and_stats(
            text, ("a",)
        )
        _check(result.kind.name == "MATCH", "decoded-secret URLs are recognised")
        _check(len(matches) == reps, "every repetition keeps exactly one secret")
        return reps, stats.secret_record_transfers + stats.secret_ordering_work

    small_reps, small_work = finalize_work(100)
    large_reps, large_work = finalize_work(200)
    _check(small_work > 0, "finalization does observable ordering work")
    _check(
        large_work <= 2 * small_work + 16,
        f"finalization maintenance work grows at most linearly: "
        f"{small_work} -> {large_work}",
    )
    _check(
        small_work < small_reps * 128,
        "finalization never revisits more than a constant per record",
    )

    incremental = recognizer.IncrementalRecognizer(("a",))
    incremental.feed(" ".join(["https://host/%61"] * 200))
    _check(
        len(incremental.decoded_secrets()) == 200,
        "incremental finalization keeps every secret",
    )
    _check(
        incremental.stats.secret_record_transfers
        + incremental.stats.secret_ordering_work
        <= 2 * (large_reps * 128),
        "incremental finalization work stays linear too",
    )

    def retirement_work(count: int) -> tuple[int, int]:
        recognizer_instance = recognizer.IncrementalRecognizer()
        tokens = [f"https://h{index:03d}/x" for index in range(count)]
        recognizer_instance.feed(" ".join(tokens))
        before = recognizer_instance.retained_matches
        offset = 0
        for token in tokens:
            offset += len(token)
            recognizer_instance.consume(offset)
            offset += 1
        _check(
            recognizer_instance.retained_matches == 0,
            "every candidate is retired after its commit",
        )
        _check(
            recognizer_instance.stats.candidate_retirement_visits == before,
            "retirement inspects each candidate record exactly once",
        )
        return before, recognizer_instance.stats.candidate_retirement_visits

    small_before, small_visits = retirement_work(50)
    large_before, large_visits = retirement_work(100)
    _check(small_before > 0 and small_visits == small_before, "retirement is output-sensitive")
    _check(
        large_visits <= 2 * small_visits + 16,
        f"candidate retirement work grows at most linearly: "
        f"{small_visits} -> {large_visits}",
    )

    # Ordering invariant: each layer records candidates in raw-start order, so
    # incremental retirement can pop the left of its own queue.
    pipeline = recognizer._Pipeline(
        recognizer._MutableStats(), incremental=True
    )
    pipeline.feed(
        "http://a git+ssh://b https://c socks5://d h%74tp://e "
        "TOKENhttps://f https://g/x/https://h"
    )
    recorded = 0
    for layer in pipeline._layer:
        starts = [match[0] for match in layer.all_matches()]
        _check(
            starts == sorted(starts),
            "layer candidate matches stay in raw-start order",
        )
        recorded += len(starts)
    _check(recorded > 0, "the ordering check observed real candidates")


def audit_scheme_separator_depth_split() -> None:
    def encode(text: str, depth: int) -> str:
        for _ in range(depth):
            text = "".join("%%%02X" % ord(character) for character in text)
        return text

    layers = recognizer._LAYERS
    for scheme_depth in range(layers):
        text = (
            encode("https", scheme_depth) + encode("://", layers) + "host"
        )
        _check(
            recognizer.recognize(text).kind.name == "MATCH",
            f"scheme depth {scheme_depth} with a depth-four separator is detected",
        )
    for text, message in (
        (
            encode("https", layers) + "://host",
            "a four-pass-encoded scheme with a literal separator is not a candidate",
        ),
        (
            encode("https", layers) + encode("://", layers) + "host",
            "a four-pass-encoded scheme and separator is not a candidate",
        ),
    ):
        _check(recognizer.recognize(text).kind.name == "NO_MATCH", message)
    capped = recognizer._Pipeline(recognizer._MutableStats())._layer[-1]
    _check(capped._scheme_cap, "the last layer caps scheme decoding")


def audit_decoded_secret_speculation() -> None:
    ordinary = recognizer._Pipeline(
        recognizer._MutableStats(), incremental=True, secrets=("TOKEN",)
    )
    ordinary.feed("%54OKEN" * 5_000)
    _check(
        ordinary.decoded_secrets() == (),
        "ordinary percent-encoded text retains no decoded secret matches",
    )
    _check(
        ordinary.stats().peak_retained_secrets <= 1,
        "decoded secret speculation stays bounded on ordinary text",
    )
    inside = recognizer._Pipeline(
        recognizer._MutableStats(), incremental=True, secrets=("TOKEN",)
    )
    inside.feed("https://host/%54OKEN/private")
    _check(
        len(recognizer._dedupe_secret_matches(inside.decoded_secrets())) == 1,
        "a decoded secret inside a supported URL is retained",
    )
    terminated = recognizer._Pipeline(
        recognizer._MutableStats(), incremental=True, secrets=("TOKEN",)
    )
    terminated.feed("https://host/%54OKEN tail")
    _check(
        len(recognizer._dedupe_secret_matches(terminated.decoded_secrets())) == 1,
        "a decoded secret ending at a raw terminator is retained",
    )


def audit_no_retained_tail() -> None:
    incremental = recognizer.IncrementalRecognizer(("TOKENht",))
    _check(not hasattr(incremental, "_tail"), "recognizer has no retained raw tail")
    _check(not hasattr(incremental, "_rebuild"), "recognizer has no rebuild path")
    incremental.feed("TOKENht")
    incremental.consume(7)
    incremental.feed("tps://host")
    match = incremental.earliest_match()
    _check(
        match is not None and match.start == 5,
        "an overlapping scheme prefix survives a commit",
    )
    _check(
        incremental.literal_secrets() == (),
        "a committed secret is not reported twice",
    )


def audit_no_raw_or_decoded_payload() -> None:
    stats = recognizer._MutableStats()
    pipeline = recognizer._Pipeline(stats, secrets=("TOKEN",))
    payload = "h%74tps%3A%2F%2F" + ("x" * 5_000)
    pipeline.feed(payload)
    # No pipeline or layer attribute may hold an input-sized string or list.
    for name, value in vars(pipeline).items():
        _check(
            not isinstance(value, (str, bytes)) or len(value) < 64,
            f"pipeline attribute {name} holds no raw payload",
        )
    for layer in pipeline._layer:
        for name in layer.__slots__:
            value = getattr(layer, name, None)
            if isinstance(value, (str, bytes)):
                _check(
                    len(value) < 64,
                    f"layer attribute {name} holds no raw payload",
                )
    recognizer_stats = pipeline.stats()
    _check(
        recognizer_stats.peak_pending_chars < 4,
        f"peak pending decoder characters {recognizer_stats.peak_pending_chars} < 4",
    )
    _check(
        recognizer_stats.peak_atom_queue <= 3,
        f"peak atom queue {recognizer_stats.peak_atom_queue} <= 3",
    )


def audit_work_is_output_sensitive() -> None:
    small = recognizer._MutableStats()
    recognizer._Pipeline(small).feed("https://h " * 1000)
    large = recognizer._MutableStats()
    recognizer._Pipeline(large).feed("https://h " * 2000)
    _check(
        large.atoms <= 2 * small.atoms + 8,
        "structural atom work grows at most linearly with input",
    )
    _check(
        large.layer_char_visits <= 2 * small.layer_char_visits + 8,
        "layer visits grow at most linearly with input",
    )


def main() -> int:
    audit_imports_and_fallback()
    audit_registration_storage()
    audit_bounded_state()
    audit_scheme_separator_depth_split()
    audit_decoded_secret_speculation()
    audit_no_retained_tail()
    audit_no_raw_or_decoded_payload()
    audit_work_is_output_sensitive()
    audit_pruning_and_coverage_work()
    audit_finalization_and_retirement_work()
    print("P2 ALLOCATION AUDIT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
