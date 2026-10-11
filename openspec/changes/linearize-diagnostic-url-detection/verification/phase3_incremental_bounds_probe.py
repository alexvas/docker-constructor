"""P3 deterministic audit: all search instances, ownership and lifetime work.

Run with PYTHONPATH="$PWD" .venv/bin/python <this file>. No elapsed timings.
"""
from unittest.mock import patch

import docker.versioning.diagnostic_recognizer as recognition
from docker.versioning.diagnostic_projection import DiagnosticProjector, PENDING_LIMIT_BYTES
from docker.versioning.model import NetworkUrlDisplay

FIELDS = ("layer_char_visits", "emissions", "atoms", "transitions", "secret_emissions",
          "speculative_record_visits", "secret_record_transfers", "secret_ordering_work",
          "candidate_retirement_visits", "prefix_checks", "failure_traversals", "automaton_outputs",
          "coverage_checks")


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("[ok]", message)


def inspect(p):
    r = p._recognizer
    assert not hasattr(r, "_tail")
    assert not any(isinstance(value, str) for name, value in vars(p).items()
                   if name != "_url_display")  # str Enum is configuration, not payload
    assert p.pending_size <= PENDING_LIMIT_BYTES
    pipe = r._pipeline
    assert len(pipe._structure) == 4
    for transducer in pipe._transducers:
        assert len(transducer._pending) <= 2
        assert all(len(unit[0]) == 1 for unit in transducer._pending)
    for matcher in pipe._layer:
        assert len(matcher._buf) <= 2
        assert len(matcher._queue) <= 3
        assert len(matcher._raw_starts) <= recognition._MAX_SCHEME_LEN + 4
        assert len(matcher._matches) <= PENDING_LIMIT_BYTES
    observers = list(pipe._decoded)
    if pipe._literal is not None:
        observers.append(pipe._literal)
    for observer in observers:
        assert len(observer._spans) <= max(observer._max_len, 1)
        assert len(observer._speculative) == len(observer._cursor._automaton.patterns)
        assert observer._retained <= PENDING_LIMIT_BYTES * max(1, len(observer._speculative))


def measure(text, *, chunk=1, host_path=False,
            secrets=("TOKENht", "%", "password done")):
    instances = []
    original = recognition._Pipeline
    def tracked(*args, **kwargs):
        instance = original(*args, **kwargs)
        instances.append(instance)
        return instance
    out = []
    with patch.object(recognition, "_Pipeline", side_effect=tracked):
        p = DiagnosticProjector(secrets, url_display=(
            NetworkUrlDisplay.HOST_PATH if host_path else NetworkUrlDisplay.REDACTED))
        for start in range(0, len(text), chunk):
            out.extend(p.feed_text(text[start:start + chunk]))
            inspect(p)
            p.take_facts()
            p.take_host_paths()
        out.extend(p.finish())
        inspect(p)
    assert len(instances) == p.work_stats.recognition_instances == 1
    assert sum(instance._stats.chars for instance in instances) == len(text)
    assert p.work_stats.encoded_bytes == len(text.encode("utf-8"))
    assert p.work_stats.buffer_appends == p.work_stats.buffer_pops
    total = sum(sum(getattr(instance.stats(), field) for field in FIELDS) for instance in instances)
    w = p.work_stats
    total += w.buffer_appends + w.buffer_pops + w.materialized_chars + w.provenance_visits
    return total, "".join(out)


def main():
    for host_path in (False, True):
        for label, make, small, large in (
            ("ordinary", lambda n: "x" * n, 3000, 6000),
            ("commits", lambda n: "pre TOKENhttps://host/%25/password done next " * n, 100, 200),
            ("overflow", lambda n: ("https://host/" + "x" * (PENDING_LIMIT_BYTES + 1)
                                   + "password done next ") * n, 2, 4),
        ):
            a, _ = measure(make(small), host_path=host_path)
            b, _ = measure(make(large), host_path=host_path)
            check(b <= 2 * a + 64, f"{label}, host-path={host_path}: cumulative work {a} -> {b}")
    for host_path in (False, True):
        first, second = ("Xhttps://host done Xhttps://hor done " * n for n in (100, 200))
        a, _ = measure(first, host_path=host_path, secrets=("Xhttps://host",))
        b, _ = measure(second, host_path=host_path, secrets=("Xhttps://host",))
        check(b <= 2 * a + 64,
              f"crossing-prefix settlement, host-path={host_path}: cumulative work {a} -> {b}")
        coarse, output = measure(first, chunk=len(first), host_path=host_path,
                                 secrets=("Xhttps://host",))
        fine, fine_output = measure(first, host_path=host_path, secrets=("Xhttps://host",))
        check(coarse == fine and output == fine_output,
              f"crossing-prefix one-character / one-feed work and text agree, host-path={host_path}")
    text = "pre TOKENhttps://host/%25/password done next " * 30
    a, output = measure(text, chunk=1)
    b, coarse = measure(text, chunk=len(text))
    check(a == b and output == coarse, "one-character / one-feed work and text agree")
    check(output == "pre <secret><redacted> next " * 30, "no discarded secret suffix or duplicate URL marker")
    print("P3 OWNERSHIP / LIFETIME AUDIT PASS")


if __name__ == "__main__":
    main()
