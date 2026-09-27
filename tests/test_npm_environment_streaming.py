"""Phase 2 — bounded redacted streaming executor (tasks 2.1–2.11).

The standalone npm assembler executor drains stdout/stderr concurrently,
decodes UTF-8 incrementally, streams committed terminal-safe prefixes without
waiting for newline, neutralizes terminal controls, appends one bounded
oversized-line marker while discarding only the suffix, retains bounded 64 KiB
tails, and serializes a single constructor-owned sink behind a non-blocking
64-chunk queue.  These tests exercise the streaming primitives directly and
through ``assemble``.
"""

from __future__ import annotations

import codecs
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR.parent))

from docker.npm_environment import (
    DISPATCHER_DRAIN_BUDGET_SECONDS,
    DockerRunExecutor,
    LockedNpmError,
    READ_CHUNK_BYTES,
    READER_FAILURE_DETAIL_BYTES,
    REDACTED,
    SINK_CALLBACK_BUDGET_SECONDS,
    SINK_QUEUE_CAPACITY,
    STREAM_STDERR,
    STREAM_STDOUT,
    TAIL_BYTES,
    TRUNCATION_NOTICE,
    ProcessResult,
    RedactingStream,
    RootSpec,
    SinkDispatcher,
    StreamChunk,
    StreamReaderFailure,
    assemble,
    assemble_environment,
    assembler_namespace_path,
    assembler_script_digest,
    collect_streams,
    compute_assembler_identity,
    dedupe_secrets,
    longest_secret_length,
    npm_policy_digest,
    preflight,
    redact_tail,
    redact_text,
)
from docker.versioning.npm_diagnostic_stream import (
    DIAGNOSTIC_LINE_LIMIT_BYTES,
    NpmDiagnosticStream,
    OVERSIZED_DIAGNOSTIC_MARKER,
    classify_npm_diagnostic,
    make_stream_factory,
    project_tail,
)
from docker.versioning.host_progress import (
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationState,
    format_failure_report,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.constructor_cli import CommandRequest, _real_dispatcher, _render
from docker.npm_environment.execution import _exit_failure
from docker.versioning.build_orchestration import (
    BuildResult,
    _host_failure_context,
)
from docker.versioning.constructor_project import ConstructorProject
from docker.versioning.dispatch_types import ExitKind
from docker.npm_environment.streaming import (
    CONTROL_SEQUENCE_LIMIT_BYTES,
    INCOMPLETE_CONTROL_SEQUENCE,
    OVERSIZED_CONTROL_SEQUENCE,
    TerminalControlNeutralizer,
    TerminalSafeLineFramer,
    _Utf8ByteDecoder,
    _visible_control,
)

_IMAGE = "sha256:" + "a" * 64
_NODE = "24.18.0"
_NPM = "11.16.0"
_PLATFORM = "linux-x64"


def _sri() -> str:
    import base64

    return "sha512-" + base64.b64encode(bytes([0xAB]) * 64).decode()


def _validated():
    raw = json.dumps(
        {
            "name": "root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {
                "": {
                    "name": "root",
                    "version": "1.0.0",
                    "dependencies": {"a": "1.0.0"},
                },
                "node_modules/a": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
                    "integrity": _sri(),
                },
            },
        }
    ).encode()
    return preflight(
        raw,
        roots=(RootSpec("a", "1.0.0"),),
        platform=_PLATFORM,
        node_version=_NODE,
        npm_version=_NPM,
    )


def _assembler():
    return compute_assembler_identity(
        image_digest=_IMAGE,
        node_version=_NODE,
        npm_version=_NPM,
        script_digest=assembler_script_digest(),
        policy_digest=npm_policy_digest(),
        platform=_PLATFORM,
    )


class FakeClock:
    """Deterministic monotonic clock for bounded-finalization tests."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _CoordinatedQueue(queue.Queue):
    """Controlled queue seam for forcing a submit/finish interleave.

    ``put_nowait`` records chunk submissions and, on the first call, pauses
    once the acceptance decision has passed so a test can race ``finish``
    against an in-flight ``submit``.  ``put`` records the sentinel so tests
    can assert enqueue ordering.
    """

    def __init__(self, maxsize: int = 0) -> None:
        super().__init__(maxsize)
        self.inside_put = threading.Event()
        self.allow_put = threading.Event()
        self.enqueue_log: list[tuple[str, ...]] = []

    def put_nowait(self, item: StreamChunk) -> None:
        if not self.allow_put.is_set():
            self.inside_put.set()
            self.allow_put.wait(2.0)
        self.enqueue_log.append(("chunk", item.text))
        super().put_nowait(item)

    def put(self, item, block: bool = True, timeout=None) -> None:
        if block:
            self.enqueue_log.append(("sentinel",))
        super().put(item, block=block, timeout=timeout)


def _pipe_pair() -> tuple[int, int]:
    return os.pipe()


class _CollectHarness:
    """Runs ``collect_streams`` against real pipes for concurrency tests."""

    def __init__(self, *, sink=None, secrets=()):
        self.out_r, self.out_w = os.pipe()
        self.err_r, self.err_w = os.pipe()
        self.sink = sink
        self.secrets = secrets
        self.capture = None
        self.error = None

    def run(self) -> None:
        try:
            self.capture = collect_streams(
                stdout_read=lambda n: os.read(self.out_r, n),
                stderr_read=lambda n: os.read(self.err_r, n),
                secrets=self.secrets,
                sink=self.sink,
            )
        except BaseException as exc:  # pragma: no cover - test aid
            self.error = exc

    def write_stdout(self, data: bytes) -> None:
        os.write(self.out_w, data)

    def write_stderr(self, data: bytes) -> None:
        os.write(self.err_w, data)

    def close_writes(self) -> None:
        """Signal EOF on both pipes (readers then finish)."""
        for fd in (self.out_w, self.err_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def close_reads(self) -> None:
        """Close read ends after the readers have exited."""
        for fd in (self.out_r, self.err_r):
            try:
                os.close(fd)
            except OSError:
                pass


class TestRedaction(unittest.TestCase):
    def test_redact_text_order_independent(self):
        secrets_sets = (
            ("abc", "abcdef", "bcde"),
            ("abcdef", "bcde", "abc"),
            ("bcde", "abc", "abcdef"),
        )
        results = {redact_text("xabcdefy", s) for s in secrets_sets}
        self.assertEqual(results, {"x<redacted>y"})

    def test_overlapping_secrets_single_longest_marker(self):
        secrets = ("abc", "abcdef", "bcde")
        out = redact_text("abcdef", secrets)
        self.assertEqual(out, "<redacted>")
        self.assertNotIn("def", out)

    def test_no_complete_secret_nor_remainder_in_output(self):
        secrets = ("abc", "abcdef", "bcde")
        for text in ("abcdef", "xabcdefy", "abcxbcdef", "abcde"):
            out = redact_text(text, secrets)
            for secret in secrets:
                self.assertNotIn(secret, out)
            self.assertNotIn("def", out)

    def test_redact_replaces_each_match_once(self):
        self.assertEqual(redact_text("aa", ("a",)), "<redacted><redacted>")
        self.assertEqual(redact_text("abab", ("ab",)), "<redacted><redacted>")


class TestIncrementalDecoder(unittest.TestCase):
    def test_two_byte_character_split_across_reads(self):
        stream = RedactingStream(())
        # The incomplete leading byte is held by the decoder, but the already
        # committed "caf" prefix is delivered without waiting for newline and
        # the completed "é" stays intact across the split.
        self.assertEqual("".join(stream.feed_bytes(b"caf\xc3")), "caf")
        chunks = stream.feed_bytes(b"\xa9 done\n")
        self.assertEqual("".join(chunks), "é done\n")

    def test_four_byte_character_split_across_reads(self):
        stream = RedactingStream(())
        # U+1F600 GRINNING FACE = F0 9F 98 80
        self.assertEqual(stream.feed_bytes(b"\xf0"), ())
        self.assertEqual(stream.feed_bytes(b"\x9f"), ())
        self.assertEqual(stream.feed_bytes(b"\x98"), ())
        chunks = stream.feed_bytes(b"\x80\n")
        self.assertEqual("".join(chunks), "😀\n")

    def test_eof_flushes_final_partial_without_newline(self):
        stream = RedactingStream(())
        # An unterminated final line is delivered promptly as its committed
        # prefix; ``finish`` only flushes residual decoder/overlap state.
        self.assertEqual(
            "".join(stream.feed_bytes(b"no trailing newline")),
            "no trailing newline",
        )
        self.assertEqual(stream.finish(), ())
        self.assertEqual(stream.tail(), "no trailing newline")


class TestUtf8ByteDecoderDifferential(unittest.TestCase):
    """The custom decoder must not change visible malformed-input behavior."""

    @staticmethod
    def _python(data: bytes) -> str:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        return decoder.decode(data, final=False) + decoder.decode(b"", final=True)

    @staticmethod
    def _custom(data: bytes) -> str:
        decoder = _Utf8ByteDecoder()
        return "".join(
            character for character, _ in decoder.feed(data)
        ) + "".join(character for character, _ in decoder.finish())

    def test_known_malformed_classes_match_python(self):
        cases = (
            b"\xe2\x82",
            b"\xf0\x9f\x92",
            b"\xe2",
            b"\xf0",
            b"\xe2\x28\xa1",
            b"\xf0\x9f\x28\xa1",
            b"\xc0\xaf",
            b"\xc1\xbf",
            b"\xe0\x80\xaf",
            b"\xf0\x80\x80\xaf",
            b"\xe0\x9f\xbf",
            b"\xed\xa0\x80",
            b"\xed\xbf\xbf",
            b"\xf4\x90\x80\x80",
            b"\xf5\x80\x80\x80",
            b"\xff\xfe\xfd",
            b"\x80\x80\x80",
            b"\xf0\x9f\x92\xa9",
            b"\xe2\x82\xac",
            b"a\xe2\x82b",
            b"\xe2\x82\xe2\x82",
            b"\xe1\x80",
            b"\xe1\x80\xe1\x80",
            b"\xe2\x82\xe2\x82\xac",
        )
        for data in cases:
            with self.subTest(data=data):
                self.assertEqual(self._python(data), self._custom(data))

    def test_exhaustive_short_inputs_match_python(self):
        for first in range(256):
            data = bytes([first])
            self.assertEqual(self._python(data), self._custom(data), data)
            for second in range(256):
                data = bytes([first, second])
                self.assertEqual(self._python(data), self._custom(data), data)

    def test_random_longer_inputs_and_every_split_match_python(self):
        import random

        rng = random.Random(20260927)
        for _ in range(1500):
            length = rng.randint(3, 9)
            data = bytes(rng.randrange(256) for _ in range(length))
            expected = self._python(data)
            self.assertEqual(expected, self._custom(data), data)
            for split in range(len(data) + 1):
                decoder = _Utf8ByteDecoder()
                left = "".join(
                    character for character, _ in decoder.feed(data[:split])
                )
                right = "".join(
                    character for character, _ in decoder.feed(data[split:])
                )
                tail = "".join(
                    character for character, _ in decoder.finish()
                )
                self.assertEqual(
                    expected, left + right + tail, (data, split)
                )


class TestRedactingStream(unittest.TestCase):
    def test_tail_bounded_to_64_kib(self):
        stream = RedactingStream(())
        # Every source line stays under the bound, so the retained tail is a
        # bounded suffix of ordinary emitted content.
        line = b"x" * 1024 + b"\n"
        for _ in range(TAIL_BYTES // 1024 + 4):
            stream.feed_bytes(line)
        stream.finish()
        tail = stream.tail()
        self.assertLessEqual(len(tail.encode("utf-8")), TAIL_BYTES)
        # The tail is a suffix of the emitted stream of single-character lines.
        self.assertTrue(tail.endswith("x\n"))
        self.assertEqual(set(tail), {"x", "\n"})

    def test_pending_overlap_bounded_by_longest_secret_minus_one(self):
        secrets = ("abc", "abcdef", "bcde")
        longest = longest_secret_length(secrets)
        stream = RedactingStream(secrets)
        for chunk in (b"x", b"ab", b"cd", b"ef", b"ghi", b"j"):
            stream.feed_bytes(chunk)
            self.assertLessEqual(stream.pending_size, longest - 1)
        stream.finish()

    def test_split_secret_across_reads_emits_one_marker(self):
        stream = RedactingStream(("SUPERSECRET",))
        out: list[str] = []
        for part in (b"before SUPERS", b"ECRET after"):
            out.extend(stream.feed_bytes(part))
        out.extend(stream.finish())
        text = "".join(out)
        self.assertNotIn("SUPERSECRET", text)
        self.assertEqual(text.count(REDACTED), 1)
        self.assertEqual(text, f"before {REDACTED} after")

    def test_prefix_not_emitted_before_match_determined(self):
        stream = RedactingStream(("abc",))
        # A candidate secret is held only until the match is determined; no
        # partial candidate is emitted before the match is known.
        self.assertEqual(stream.feed_bytes(b"ab"), ())
        self.assertEqual(stream.feed_bytes(b"c"), (REDACTED,))
        self.assertEqual(stream.finish(), ())

    def test_secret_spanning_line_boundary(self):
        stream = RedactingStream(("ab\nc",))
        # The redactor holds the candidate prefix across the framed boundary
        # so a secret containing a newline is still matched exactly once.
        self.assertEqual(stream.feed_bytes(b"ab\n"), ())
        self.assertEqual(stream.feed_bytes(b"c\n"), (REDACTED,))
        self.assertEqual(stream.finish(), ("\n",))

    def test_line_exactly_at_limit_is_emitted_unchanged(self):
        stream = RedactingStream(())
        line = b"a" * DIAGNOSTIC_LINE_LIMIT_BYTES
        chunks = list(stream.feed_bytes(line + b"\n"))
        self.assertEqual("".join(chunks), line.decode("utf-8") + "\n")

    def test_line_one_byte_over_appends_one_oversized_marker(self):
        stream = RedactingStream(())
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        line = prefix + b"S" * 100
        chunks = list(stream.feed_bytes(line + b"\n"))
        joined = "".join(chunks)
        # The committed prefix is delivered, exactly one marker is appended,
        # and the discarded suffix is neither emitted nor retained.
        self.assertEqual(joined.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertTrue(joined.startswith(prefix.decode("utf-8")))
        self.assertNotIn("S", joined)
        stream.finish()
        self.assertNotIn("S", stream.tail())
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, stream.tail())

    def test_oversized_line_drains_then_recovers_on_next_line(self):
        stream = RedactingStream(())
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        first = "".join(stream.feed_bytes(prefix + b"S" * 100))
        # Overflow notification is immediate, before the terminating newline.
        self.assertTrue(first.startswith(prefix.decode("utf-8")))
        self.assertEqual(first.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        # Still draining: bytes before the terminating newline are discarded.
        self.assertEqual(stream.feed_bytes(b"more discarded\n"), ("\n",))
        # Normal processing resumes on the following source line.
        self.assertEqual(stream.feed_bytes(b"recovered\n"), ("recovered", "\n"))
        stream.finish()
        joined = stream.tail()
        self.assertNotIn("S", joined)
        self.assertNotIn("more discarded", joined)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, joined)
        self.assertIn("recovered", joined)

    def test_unterminated_oversized_line_appends_one_marker_at_eof(self):
        stream = RedactingStream(())
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        emitted = "".join(stream.feed_bytes(prefix + b"S" * 100))
        # The marker is appended when the line first overflows; stream
        # termination while the suffix is being discarded adds no second one.
        self.assertEqual(emitted.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertEqual(stream.finish(), ())
        self.assertNotIn("S", stream.tail())

    def test_redact_text_matches_stream_for_whole_input(self):
        secrets = ("abc", "abcdef", "bcde", "SUPERSECRET")
        for text in ("abcdef", "xabcdey", "before SUPERSECRET after", "no-secret"):
            stream = RedactingStream(secrets)
            emitted = "".join(stream.feed_bytes(text.encode("utf-8")))
            emitted += "".join(stream.finish())
            self.assertEqual(emitted, redact_text(text, secrets), text)


_CONTROL_STRINGS = {
    "csi": b"\x1b[31m",
    "csi_c1": "\u009b31m".encode(),
    "osc_bel": b"\x1b]0;title\x07",
    "osc_st": b"\x1b]0;title\x1b\\",
    "osc_c1_bel": "\u009d0;title\x07".encode(),
    "osc_c1_st": "\u009d0;title\u009c".encode(),
    "dcs_st": b"\x1bP1;2|payload\x1b\\",
    "dcs_c1": "\u0090payload\u009c".encode(),
    "pm_st": b"\x1b^payload\x1b\\",
    "apc_st": b"\x1b_payload\x1b\\",
    "sos_st": b"\x1bXpayload\x1b\\",
}

#: C1 code points that introduce a control sequence rather than render alone.
_C1_INTRODUCERS = {0x90, 0x98, 0x9B, 0x9D, 0x9E, 0x9F}


class TestTerminalControlNeutralizer(unittest.TestCase):
    def _neutralize(self, parts=(), *, limit=None):
        kwargs = {} if limit is None else {"limit_bytes": limit}
        neutralizer = TerminalControlNeutralizer(**kwargs)
        output: list[str] = []
        for part in parts:
            output.extend(neutralizer.feed_bytes(part))
        output.extend(neutralizer.finish())
        return "".join(output)

    def _exact_and_over(self, introducer: bytes, terminator: bytes, content=b"x"):
        limit = CONTROL_SEQUENCE_LIMIT_BYTES
        fixed = len(introducer) + len(terminator)
        exact = introducer + content * (limit - fixed) + terminator
        over = introducer + content * (limit - fixed + 1) + terminator
        self.assertEqual(limit, len(exact))
        self.assertEqual(limit + 1, len(over))
        return exact, over

    # -- 3. complete control forms render inert --

    def test_complete_control_strings_render_as_inert_literals(self):
        for name, sequence in _CONTROL_STRINGS.items():
            with self.subTest(name=name):
                expected = _visible_control(sequence.decode("utf-8"))
                self.assertEqual(expected, self._neutralize((sequence,)))

    def test_all_string_terminator_variants(self):
        variants = (
            b"\x1b]title\x07",
            b"\x1b]title\x1b\\",
            "\u009dtitle\x07".encode(),
            "\u009dtitle\u009c".encode(),
        )
        for sequence in variants:
            with self.subTest(sequence=sequence):
                self.assertEqual(
                    _visible_control(sequence.decode("utf-8")),
                    self._neutralize((sequence,)),
                )

    def test_preserves_unicode_and_tabs(self):
        text = "\u03c0\ttext \u2764 end"
        self.assertEqual(text, self._neutralize((text.encode("utf-8"),)))

    def test_cr_backspace_nul_del_and_c1_render_inert(self):
        for character in ("\r", "\x08", "\x00", "\x7f", "\x84", "\x85"):
            with self.subTest(code=hex(ord(character))):
                self.assertEqual(
                    _visible_control(character),
                    self._neutralize((character.encode("utf-8"),)),
                )

    def test_every_remaining_c0_control_renders_inert(self):
        for code in list(range(0x00, 0x20)) + [0x7F]:
            character = chr(code)
            if character in {"\n", "\t", "\x1b"}:
                continue
            with self.subTest(code=hex(code)):
                self.assertEqual(
                    _visible_control(character),
                    self._neutralize((character.encode("utf-8"),)),
                )

    def test_every_remaining_c1_control_renders_inert(self):
        for code in range(0x80, 0xA0):
            if code in _C1_INTRODUCERS:
                continue
            character = chr(code)
            with self.subTest(code=hex(code)):
                self.assertEqual(
                    _visible_control(character),
                    self._neutralize((character.encode("utf-8"),)),
                )

    # -- 4. arbitrary fragmentation --

    def test_every_byte_split_matches_unsplit(self):
        sequences = list(_CONTROL_STRINGS.values()) + [
            b"a\x1b[31mb",
            b"pre\x1b]t\x07post",
            "\u03c0\x1b[1;2m\u754c".encode("utf-8"),
        ]
        for sequence in sequences:
            unsplit = self._neutralize((sequence,))
            for split in range(len(sequence) + 1):
                with self.subTest(sequence=sequence, split=split):
                    self.assertEqual(
                        unsplit,
                        self._neutralize((sequence[:split], sequence[split:])),
                    )
            bytewise = tuple(bytes([byte]) for byte in sequence)
            self.assertEqual(unsplit, self._neutralize(bytewise))

    # -- 5. exact size boundaries --

    def test_size_boundaries_for_every_control_form(self):
        bel = b"\x07"
        st_c1 = "\u009c".encode()
        st_esc = b"\x1b\\"
        forms = (
            (b"\x1b]", (bel, st_c1, st_esc)),
            ("\u009d".encode(), (bel, st_c1, st_esc)),
            (b"\x1bP", (st_c1, st_esc)),
            (b"\x1b^", (st_c1, st_esc)),
            (b"\x1b_", (st_c1, st_esc)),
            (b"\x1bX", (st_c1, st_esc)),
            ("\u0090".encode(), (st_c1, st_esc)),
            ("\u009e".encode(), (st_c1, st_esc)),
            ("\u009f".encode(), (st_c1, st_esc)),
            ("\u0098".encode(), (st_c1, st_esc)),
        )
        for introducer, terminators in forms:
            for terminator in terminators:
                exact, over = self._exact_and_over(introducer, terminator)
                with self.subTest(introducer=introducer, terminator=terminator):
                    self.assertEqual(
                        _visible_control(exact.decode("utf-8")),
                        self._neutralize((exact,)),
                    )
                    self.assertEqual(
                        OVERSIZED_CONTROL_SEQUENCE, self._neutralize((over,))
                    )
        for introducer in (b"\x1b[", "\u009b".encode()):
            exact, over = self._exact_and_over(introducer, b"m", content=b"1")
            with self.subTest(introducer=introducer):
                self.assertEqual(
                    _visible_control(exact.decode("utf-8")),
                    self._neutralize((exact,)),
                )
                self.assertEqual(
                    OVERSIZED_CONTROL_SEQUENCE, self._neutralize((over,))
                )

    def test_bel_terminates_osc_only(self):
        st = b"\x1b\\"
        for introducer in (b"\x1b]", "\u009d".encode()):
            with self.subTest(introducer=introducer):
                self.assertEqual(
                    _visible_control((introducer + b"payload\x07after").decode()),
                    self._neutralize((introducer + b"payload\x07after",)),
                )
        for introducer in (
            b"\x1bP",
            b"\x1bX",
            b"\x1b^",
            b"\x1b_",
            "\u0090".encode(),
            "\u0098".encode(),
            "\u009e".encode(),
            "\u009f".encode(),
        ):
            with self.subTest(introducer=introducer):
                # BEL is ordinary string content; the sequence is still open.
                output = self._neutralize((introducer + b"payload\x07",))
                self.assertEqual(1, output.count(INCOMPLETE_CONTROL_SEQUENCE))
                # ST terminates it and processing resumes.
                self.assertEqual(
                    _visible_control(
                        (introducer + b"payload\x07" + st + b"after").decode()
                    ),
                    self._neutralize(
                        (introducer + b"payload\x07" + st + b"after",)
                    ),
                )

    def test_tiny_limit_introducer_fails_closed(self):
        output = self._neutralize(("\u009d".encode(), b"x\x07after"), limit=1)
        self.assertEqual(OVERSIZED_CONTROL_SEQUENCE + "after", output)
        output = self._neutralize((b"\x1b]payload\x07tail",), limit=1)
        self.assertEqual(OVERSIZED_CONTROL_SEQUENCE + "tail", output)

    def test_oversized_emits_one_marker_and_resumes_after_terminator(self):
        limit = CONTROL_SEQUENCE_LIMIT_BYTES
        st_esc = b"\x1b\\"
        forms = (
            (b"\x1b]", b"\x07"),
            (b"\x1b]", st_esc),
            (b"\x1bP", st_esc),
            (b"\x1b^", st_esc),
            (b"\x1b_", st_esc),
            (b"\x1bX", st_esc),
            (b"\x1b[", b"m"),
        )
        for introducer, terminator in forms:
            content = b"1" if introducer.endswith(b"[") else b"x"
            data = (
                b"before"
                + introducer
                + content * limit
                + terminator
                + b"after"
            )
            with self.subTest(introducer=introducer, terminator=terminator):
                output = self._neutralize((data,))
                self.assertEqual(1, output.count(OVERSIZED_CONTROL_SEQUENCE))
                self.assertEqual(
                    "before" + OVERSIZED_CONTROL_SEQUENCE + "after", output
                )

    def test_oversized_discard_ignores_bel_until_st(self):
        limit = CONTROL_SEQUENCE_LIMIT_BYTES
        for introducer in (b"\x1bP", b"\x1bX", b"\x1b^", b"\x1b_"):
            data = (
                b"before"
                + introducer
                + b"x" * limit
                + b"\x07discarded"
                + b"\x1b\\"
                + b"after"
            )
            with self.subTest(introducer=introducer):
                output = self._neutralize((data,))
                self.assertEqual(1, output.count(OVERSIZED_CONTROL_SEQUENCE))
                self.assertEqual(
                    "before" + OVERSIZED_CONTROL_SEQUENCE + "after", output
                )

    def test_oversized_osc_discard_resumes_at_bel(self):
        limit = CONTROL_SEQUENCE_LIMIT_BYTES
        data = b"before\x1b]" + b"x" * limit + b"\x07after"
        self.assertEqual(
            "before" + OVERSIZED_CONTROL_SEQUENCE + "after",
            self._neutralize((data,)),
        )

    # -- 6. EOF in every state --

    def test_eof_incomplete_states_emit_one_incomplete_marker(self):
        incomplete = (
            b"\x1b",
            b"\x1b[31",
            b"\x1b]title",
            b"\x1bPpayload",
            b"\x1b^payload",
            b"\x1b_payload",
            b"\x1bXpayload",
            "\u009b31".encode(),
            "\u009dtitle".encode(),
            b"\x1b]title\x1b",
            b"\x1bPpayload\x1b",
        )
        for data in incomplete:
            with self.subTest(data=data):
                output = self._neutralize((data,))
                self.assertEqual(1, output.count(INCOMPLETE_CONTROL_SEQUENCE))
                self.assertNotIn(OVERSIZED_CONTROL_SEQUENCE, output)

    def test_eof_oversized_states_emit_only_oversized_marker(self):
        limit = CONTROL_SEQUENCE_LIMIT_BYTES
        oversized = (
            b"\x1b[" + b"1" * (limit + 10),
            b"\x1b]" + b"x" * (limit + 10),
            b"\x1b]" + b"x" * (limit + 10) + b"\x1b",
            b"\x1bP" + b"x" * (limit + 10) + b"\x1b",
        )
        for data in oversized:
            with self.subTest(prefix=bytes(data[:8])):
                output = self._neutralize((data,))
                self.assertEqual(1, output.count(OVERSIZED_CONTROL_SEQUENCE))
                self.assertNotIn(INCOMPLETE_CONTROL_SEQUENCE, output)

    # -- 1. raw-byte accounting for malformed UTF-8 --

    def test_invalid_utf8_bytes_count_once_each(self):
        limit = 8
        exact = b"\x1b[" + b"\xff" * (limit - 3) + b"m"
        self.assertEqual(limit, len(exact))
        self.assertEqual(
            _visible_control(exact.decode("utf-8", "replace")),
            self._neutralize((exact,), limit=limit),
        )
        over = b"\x1b[" + b"\xff" * (limit - 2) + b"m"
        self.assertEqual(
            OVERSIZED_CONTROL_SEQUENCE, self._neutralize((over,), limit=limit)
        )

    def test_invalid_utf8_accounting_is_chunk_boundary_invariant(self):
        limit = 8
        data = (
            b"\x1b["
            + b"\xff" * 4
            + b"m"
            + "tail\u03c0".encode("utf-8")
            + b"\x1b]"
            + b"\xfe" * 3
            + b"\x07end"
        )
        unsplit = self._neutralize((data,), limit=limit)
        self.assertNotIn(OVERSIZED_CONTROL_SEQUENCE, unsplit)
        for split in range(len(data) + 1):
            with self.subTest(split=split):
                self.assertEqual(
                    unsplit,
                    self._neutralize((data[:split], data[split:]), limit=limit),
                )
        bytewise = tuple(bytes([byte]) for byte in data)
        self.assertEqual(unsplit, self._neutralize(bytewise, limit=limit))

    def test_npm_diagnostic_stream_uses_terminal_neutralization(self) -> None:
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes(b"npm warn \x1b[31mbad\x1b[0m\n")
        finalized = _finalized(chunks)
        self.assertEqual(1, len(finalized))
        self.assertEqual(r"npm warn \x1b[31mbad\x1b[0m", finalized[0].text)
        self.assertEqual(
            r"npm warn \x1b[31mbad\x1b[0m",
            project_tail("npm warn \x1b[31mbad\x1b[0m"),
        )


class _ScriptedReader:
    """Reader returning scripted byte parts, then an error, then EOF."""

    def __init__(self, parts, *, error=None, on_first=None):
        self._parts = list(parts)
        self._error = error
        self._on_first = on_first
        self._first = True

    def __call__(self, _max_bytes):
        if self._first and self._on_first is not None:
            self._on_first()
            self._first = False
        if self._parts:
            return self._parts.pop(0)
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        return b""


def _joined_text(chunks) -> str:
    return "".join(
        chunk.text if isinstance(chunk, StreamChunk) else chunk for chunk in chunks
    )


def _finalized(chunks) -> list[StreamChunk]:
    """Return only finalized complete-line events from *chunks*."""
    return [
        chunk
        for chunk in chunks
        if isinstance(chunk, StreamChunk) and chunk.finalized
    ]


class TestTerminalNeutralizationLineFraming(unittest.TestCase):
    """Task 7 — 64 KiB line framing over neutralized output."""

    def _feed_all(self, stream, data, *, chunk=4096):
        chunks = []
        for start in range(0, len(data), chunk):
            chunks.extend(stream.feed_bytes(data[start : start + chunk]))
        return chunks

    def test_exactly_64_kib_line_is_emitted(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        stream = NpmDiagnosticStream("stdout")
        content = b"a " * (limit // 2)
        self.assertEqual(limit, len(content))
        chunks = self._feed_all(stream, content + b"\n")
        chunks.extend(stream.finish())
        finalized = _finalized(chunks)
        self.assertEqual(1, len(finalized))
        self.assertEqual(content.decode("ascii"), finalized[0].text)
        # The committed prefix was already observable before the newline.
        self.assertTrue(any(not chunk.finalized for chunk in chunks))
        self.assertEqual(0, stream.pending_line_bytes)

    def test_one_byte_over_appends_oversized_line_marker(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        stream = NpmDiagnosticStream("stdout")
        content = b"a " * (limit // 2) + b"a"
        self.assertEqual(limit + 1, len(content))
        chunks = self._feed_all(stream, content + b"\n")
        chunks.extend(stream.finish())
        finalized = _finalized(chunks)
        self.assertEqual(1, len(finalized))
        # The committed prefix is delivered and exactly one marker is appended.
        self.assertTrue(
            finalized[0].text.startswith(content[:limit].decode("ascii"))
        )
        self.assertTrue(
            finalized[0].text.endswith(OVERSIZED_DIAGNOSTIC_MARKER)
        )
        self.assertEqual(0, stream.pending_line_bytes)

    def test_overflow_drains_then_recovers_on_next_line(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        stream = NpmDiagnosticStream("stdout")
        prefix = b"a " * (limit // 2)
        oversized = prefix + b"S" * 100
        before = list(self._feed_all(stream, oversized))
        # The committed prefix and the appended marker are observable
        # immediately, before any newline, and all are non-finalized.
        self.assertTrue(before)
        self.assertTrue(all(not chunk.finalized for chunk in before))
        self.assertEqual(
            prefix.decode("ascii") + OVERSIZED_DIAGNOSTIC_MARKER,
            _joined_text(before),
        )
        self.assertEqual(0, stream.pending_line_bytes)
        self.assertLessEqual(stream.pending_sanitizer_bytes, 8 * 1024)
        self.assertEqual((), tuple(self._feed_all(stream, b"b " * 10000)))
        self.assertLessEqual(stream.pending_sanitizer_bytes, 8 * 1024)
        chunks = self._feed_all(stream, b"\nnext line\n")
        finalized = _finalized(chunks)
        texts = [c.text for c in finalized]
        self.assertEqual(2, len(texts))
        # The committed prefix and one appended marker reach the real sink.
        self.assertTrue(texts[0].startswith(prefix.decode("ascii")))
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, texts[0])
        self.assertNotIn("S", texts[0])
        self.assertNotIn("b b b", texts[0])
        self.assertEqual("next line", texts[1])
        self.assertEqual(0, stream.pending_line_bytes)

    def test_controls_and_multibyte_unicode_near_boundary(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes("\u03c0\x1b[31mred\x1b[0m\n".encode("utf-8"))
        text = _joined_text(chunks)
        self.assertNotIn("\x1b", text)
        self.assertIn(r"\x1b[31m", text)
        self.assertIn("\u03c0", text)

    def test_line_limit_is_measured_before_control_expansion(self):
        limit = 8
        stream = NpmDiagnosticStream("stdout", line_limit=limit)
        chunks = list(stream.feed_bytes(b"\x1b" * limit + b"\n"))
        chunks.extend(stream.finish())
        texts = [c.text for c in _finalized(chunks)]
        # ``\x1b`` expands to four visible characters; measuring the line
        # after expansion would overflow here.  The source limit does not.
        self.assertNotIn(OVERSIZED_DIAGNOSTIC_MARKER, texts)
        self.assertIn(r"\x1b", texts[0])
        stream = NpmDiagnosticStream("stdout", line_limit=limit)
        chunks = list(stream.feed_bytes(b"\x1b" * (limit + 1) + b"\n"))
        chunks.extend(stream.finish())
        texts = [c.text for c in _finalized(chunks)]
        self.assertEqual(1, len(texts))
        self.assertTrue(texts[0].endswith(OVERSIZED_DIAGNOSTIC_MARKER))
        self.assertIn(r"\x1b", texts[0])

    def test_embedded_newline_inside_control_string_does_not_merge_lines(self):
        for introducer, terminator in (
            (b"\x1b]", b"\x07"),
            (b"\x1bP", b"\x1b\\"),
        ):
            with self.subTest(introducer=introducer):
                stream = NpmDiagnosticStream("stdout")
                data = introducer + b"title\npayload" + terminator + b"\n"
                chunks = list(stream.feed_bytes(data))
                chunks.extend(stream.finish())
                texts = [c.text for c in _finalized(chunks)]
                self.assertEqual(2, len(texts))
                self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, texts[0])
                self.assertNotIn("payload", texts[0])
                self.assertIn("payload", texts[1])


class TestCommittedPrefixStreaming(unittest.TestCase):
    """Task 2.8/2.9 — committed prefixes, overflow append, and recovery.

    Both collector paths stream terminal-safe committed prefixes without
    waiting for a newline.  When a source line first exceeds its bound exactly
    one marker is appended after the already-emitted prefix, only the suffix is
    discarded through the next newline (or stream termination), and processing
    recovers afterward.  Stream termination never adds a second marker.
    """

    @staticmethod
    def _texts(chunks) -> str:
        return "".join(
            chunk if isinstance(chunk, str) else chunk.text for chunk in chunks
        )

    @staticmethod
    def _prefixes(chunks) -> str:
        """Text delivered on the live/tail channel before any newline.

        The default path returns plain strings (all committed prefixes); the
        structured path returns both prefixes and finalized lines, and only
        the non-finalized prefixes belong to the live/tail channel.
        """
        return "".join(
            chunk if isinstance(chunk, str) else chunk.text
            for chunk in chunks
            if isinstance(chunk, str) or not chunk.finalized
        )

    def test_shared_framer_emits_prefix_without_newline(self):
        framer = TerminalSafeLineFramer()
        self.assertEqual(
            "".join(framer.feed_bytes(b"partial line")), "partial line"
        )
        self.assertEqual(framer.pending_line_bytes, len(b"partial line"))
        self.assertEqual(framer.finish(), ())

    def test_default_path_delivers_prefix_without_newline(self):
        stream = RedactingStream(())
        self.assertEqual(
            "".join(stream.feed_bytes(b"no newline yet")), "no newline yet"
        )
        self.assertEqual(stream.tail(), "no newline yet")

    def test_structured_path_delivers_prefix_without_newline(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes(b"no newline yet ")
        self.assertTrue(chunks)
        self.assertTrue(all(not chunk.finalized for chunk in chunks))
        self.assertEqual(self._texts(chunks), "no newline yet ")
        self.assertEqual(stream.tail(), "no newline yet ")

    def test_overflow_appends_one_marker_and_discards_suffix_both_paths(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        prefix = b"pad " * (limit // 4)
        # The first byte over the bound appends the marker; the remaining
        # suffix must never be delivered or retained.
        oversized = prefix + b"DISCARDED" * 100
        for factory in (
            lambda: RedactingStream(()),
            lambda: NpmDiagnosticStream("stdout", ()),
        ):
            with self.subTest(factory=factory):
                stream = factory()
                # No newline yet: the committed prefix and the appended marker
                # are already observable on the live/tail channel.
                before = self._prefixes(stream.feed_bytes(oversized))
                self.assertIn(prefix[:8].decode("ascii"), before)
                self.assertEqual(
                    before.count(OVERSIZED_DIAGNOSTIC_MARKER), 1
                )
                self.assertNotIn("DISCARDED", before)
                stream.finish()
                # Stream termination never adds a second marker.
                self.assertEqual(
                    stream.tail().count(OVERSIZED_DIAGNOSTIC_MARKER), 1
                )
                self.assertNotIn("DISCARDED", stream.tail())

    def test_newline_preserves_record_boundary_and_recovers_both_paths(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        prefix = b"pad " * (limit // 4)
        payload = prefix + b"DISCARDED" * 100 + b"\nrecovered line\n"
        for factory in (
            lambda: RedactingStream(()),
            lambda: NpmDiagnosticStream("stdout", ()),
        ):
            with self.subTest(factory=factory):
                stream = factory()
                emitted = self._prefixes(stream.feed_bytes(payload))
                self.assertEqual(
                    emitted.count(OVERSIZED_DIAGNOSTIC_MARKER), 1
                )
                self.assertNotIn("DISCARDED", emitted)
                self.assertIn("recovered line", emitted)
                tail = stream.tail()
                self.assertIn("recovered line", tail)
                # The newline is preserved as the record boundary for both
                # the discarded record and the recovered one.
                self.assertEqual(tail.count("\n"), 2)
                self.assertEqual(
                    tail.count(OVERSIZED_DIAGNOSTIC_MARKER), 1
                )
                self.assertNotIn("DISCARDED", tail)

    def test_exactly_at_limit_is_delivered_through_both_paths(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        # Space-separated tokens stay under the projector's independent 8 KiB
        # pending-token bound while the source line fills the 64 KiB line bound.
        line = b"z " * (limit // 2)
        self.assertEqual(len(line), limit)
        for factory in (
            lambda: RedactingStream(()),
            lambda: NpmDiagnosticStream("stdout", ()),
        ):
            with self.subTest(factory=factory):
                stream = factory()
                before = self._prefixes(stream.feed_bytes(line))
                self.assertEqual(before.count("z"), limit // 2)
                self.assertNotIn(OVERSIZED_DIAGNOSTIC_MARKER, before)
                stream.feed_bytes(b"\n")
                stream.finish()
                self.assertNotIn(
                    OVERSIZED_DIAGNOSTIC_MARKER, stream.tail()
                )
                # The retained tail stays within its fixed byte bound.
                self.assertLessEqual(
                    len(stream.tail().encode("utf-8")), TAIL_BYTES
                )

    def test_overflow_discards_pending_control_sequence(self):
        # A pending control string at the tail of the line must be discarded
        # when the line overflows; no ambiguous control payload escapes and
        # the fixed marker is sufficient.
        limit = 64
        framer = TerminalSafeLineFramer(line_limit_bytes=limit)
        pending = b"x" * (limit - 3) + b"\x1b]0;secret-payload"
        emitted = "".join(framer.feed_bytes(pending))
        self.assertEqual(emitted.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertEqual(emitted.count("x"), limit - 3)
        self.assertNotIn("\x1b", emitted)
        self.assertNotIn("secret-payload", emitted)
        self.assertEqual("".join(framer.finish()), "")


class TestTerminalNeutralizationFailurePaths(unittest.TestCase):
    """Task 8 — neutralizer finalization under failure and cancellation."""

    def _recording_factory(self):
        created: dict[str, NpmDiagnosticStream] = {}

        def factory(tag):
            stream = NpmDiagnosticStream(tag)
            created[tag] = stream
            return stream

        return created, factory

    def test_normal_eof_finalizes_incomplete_control(self):
        received: list = []
        created, factory = self._recording_factory()
        capture = collect_streams(
            stdout_read=_ScriptedReader([b"before\x1b]unterminated"]),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            stream_factory=factory,
        )
        text = _joined_text(received)
        self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, text)
        self.assertNotIn("\x1b", text)
        for stream in created.values():
            self.assertNotIn("\x1b", stream.tail())
        self.assertNotIn("\x1b", capture.stdout_tail)
        self.assertNotIn("\x1b", capture.stderr_tail)

    def test_cancellation_finalizes_incomplete_control(self):
        received: list = []
        abort_event = threading.Event()
        created, factory = self._recording_factory()
        collect_streams(
            stdout_read=_ScriptedReader([b"partial\x1b]"], on_first=abort_event.set),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            stream_factory=factory,
            abort_event=abort_event,
        )
        text = _joined_text(received)
        self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, text)
        self.assertNotIn("\x1b", text)
        for stream in created.values():
            self.assertNotIn("\x1b", stream.tail())

    def test_reader_failure_keeps_output_and_detail_terminal_safe(self):
        received: list = []
        created, factory = self._recording_factory()
        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=_ScriptedReader(
                    [b"\x1b[31mred"], error=RuntimeError("boom")
                ),
                stderr_read=_ScriptedReader([]),
                sink=received.append,
                stream_factory=factory,
            )
        text = _joined_text(received)
        self.assertNotIn("\x1b", text)
        self.assertIn(r"\x1b[31m", text)
        self.assertNotIn("\x1b", str(ctx.exception))
        for stream in created.values():
            self.assertNotIn("\x1b", stream.tail())

    def test_sibling_reader_failure_still_neutralizes_output(self):
        received: list = []
        created, factory = self._recording_factory()
        with self.assertRaises(StreamReaderFailure):
            collect_streams(
                stdout_read=_ScriptedReader([], error=RuntimeError("boom")),
                stderr_read=_ScriptedReader([b"ok\x1b]st\x07"]),
                sink=received.append,
                stream_factory=factory,
            )
        text = _joined_text(received)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn(r"\x1b]st", text)
        for stream in created.values():
            self.assertNotIn("\x1b", stream.tail())


class TestDefaultCollectorFailurePaths(unittest.TestCase):
    """Default-path (``RedactingStream``) cancellation and reader failure."""

    @staticmethod
    def _assert_no_raw_controls(case: unittest.TestCase, text: str) -> None:
        for control in ("\x1b", "\x07", "\x08", "\r", "\x00", "\x9b", "\x9d"):
            case.assertNotIn(control, text)

    def test_cancellation_finalizes_incomplete_control(self):
        received: list[StreamChunk] = []
        abort_event = threading.Event()
        capture = collect_streams(
            stdout_read=_ScriptedReader(
                [b"partial\x1b]"], on_first=abort_event.set
            ),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            abort_event=abort_event,
        )
        text = _joined_text(received)
        self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, text)
        self._assert_no_raw_controls(self, text)
        self._assert_no_raw_controls(self, capture.stdout_tail)
        self._assert_no_raw_controls(self, capture.stderr_tail)

    def test_cancellation_finalizes_oversized_line(self):
        received: list[StreamChunk] = []
        abort_event = threading.Event()
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = prefix + b"S" * 100
        capture = collect_streams(
            stdout_read=_ScriptedReader([oversized], on_first=abort_event.set),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            abort_event=abort_event,
        )
        joined = "".join(chunk.text for chunk in received)
        # The committed prefix and exactly one marker are delivered; the
        # discarded suffix never is and cancellation adds no second marker.
        self.assertEqual(joined.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertIn("Q", joined)
        self.assertNotIn("S", joined)
        self.assertNotIn("S", capture.stdout_tail)
        self._assert_no_raw_controls(self, capture.stdout_tail)

    def test_reader_failure_after_incomplete_control_fails_closed(self):
        received: list[StreamChunk] = []
        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=_ScriptedReader(
                    [b"before\x1b]unterminated"],
                    error=RuntimeError("boom"),
                ),
                stderr_read=_ScriptedReader([]),
                sink=received.append,
            )
        text = _joined_text(received)
        self.assertIn("before", text)
        self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, text)
        self.assertNotIn("unterminated", text)
        self._assert_no_raw_controls(self, text)
        self._assert_no_raw_controls(self, str(ctx.exception))
        self.assertLessEqual(
            len(ctx.exception.detail.encode("utf-8")),
            READER_FAILURE_DETAIL_BYTES,
        )

    def test_reader_failure_after_oversized_input_stays_bounded(self):
        received: list[StreamChunk] = []
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = prefix + b"S" * 100 + b"\n"
        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=_ScriptedReader(
                    [oversized], error=RuntimeError("boom")
                ),
                stderr_read=_ScriptedReader([]),
                sink=received.append,
            )
        text = _joined_text(received)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, text)
        self.assertIn("Q", text)
        self.assertNotIn("S", text)
        self._assert_no_raw_controls(self, text)
        self.assertLessEqual(
            len(ctx.exception.detail.encode("utf-8")),
            READER_FAILURE_DETAIL_BYTES,
        )

    def test_sibling_reader_continues_after_failure(self):
        received: list[StreamChunk] = []
        with self.assertRaises(StreamReaderFailure):
            collect_streams(
                stdout_read=_ScriptedReader(
                    [b"\x1b[31mred\n"], error=RuntimeError("boom")
                ),
                stderr_read=_ScriptedReader([b"ok\x1b]st\x07\n"]),
                sink=received.append,
            )
        stdout_text = "".join(
            c.text for c in received if c.stream == STREAM_STDOUT
        )
        stderr_text = "".join(
            c.text for c in received if c.stream == STREAM_STDERR
        )
        self._assert_no_raw_controls(self, stdout_text)
        self._assert_no_raw_controls(self, stderr_text)
        self.assertIn(r"\x1b[31m", stdout_text)
        self.assertIn("ok", stderr_text)


class _TextRecordingRenderer:
    """Minimal real presentation renderer capturing every rendered text."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def set_status(self, text: str) -> None:
        self.texts.append(text)

    def set_slot(self, text: str) -> None:
        self.texts.append(text)

    def clear_slot(self) -> None:
        pass

    def clear_status(self) -> None:
        pass

    def clear_all(self) -> None:
        pass

    def durable(self, text: str) -> None:
        self.texts.append(text)

    def finalize_diagnostic(
        self, text: str, *, restore_status: str | None
    ) -> None:
        self.texts.append(text)


class TestTerminalNeutralizationRouteAudit(unittest.TestCase):
    """Task 9 — raw bytes through both real collector paths and presentation.

    The structured path (``make_stream_factory`` → ``NpmDiagnosticStream``)
    and the default path (no ``stream_factory`` → ``RedactingStream``) are
    both exercised; neither input is pre-neutralized.
    """

    # Unsafe source with CSI, OSC, C0 controls, Unicode, and an incomplete
    # control string.  Nothing here is pre-neutralized.
    RAW = (
        "npm \x1b[31merror\x1b[0m \x1b]0;title\x07 \u03c0\x08\r\n"
        "npm \x1b]unterminated\n"
        "npm DCS \x1bPpayload\x07body\x1b\\ done\n"
    ).encode("utf-8")

    # A DCS payload far beyond the control-sequence bound whose BEL must not
    # terminate the discard; only ST ends it.
    OVERSIZED = (
        b"npm "
        + b"\x1bP"
        + b"x" * (CONTROL_SEQUENCE_LIMIT_BYTES + 32)
        + b"\x07tail\x1b\\ after\n"
    )

    def _collect(self, parts, *, display=NetworkUrlDisplay.REDACTED):
        received: list[StreamChunk] = []
        capture = collect_streams(
            stdout_read=_ScriptedReader(list(parts)),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            stream_factory=make_stream_factory(),
            network_url_display=display,
        )
        return received, capture

    def _collect_default(self, parts, *, display=NetworkUrlDisplay.REDACTED):
        received: list[StreamChunk] = []
        capture = collect_streams(
            stdout_read=_ScriptedReader(list(parts)),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
            network_url_display=display,
        )
        return received, capture

    @staticmethod
    def _assert_terminal_safe(case: unittest.TestCase, text: str) -> None:
        for control in ("\x1b", "\x07", "\x08", "\r", "\x00", "\x9b", "\x9d"):
            case.assertNotIn(control, text)

    def test_structured_collector_output_is_terminal_safe_in_every_mode(self):
        for display in NetworkUrlDisplay:
            with self.subTest(display=display):
                received, capture = self._collect(
                    [self.RAW, self.OVERSIZED], display=display
                )
                self.assertTrue(received)
                for chunk in received:
                    self._assert_terminal_safe(self, chunk.text)
                self._assert_terminal_safe(self, capture.stdout_tail)
                self._assert_terminal_safe(self, capture.stderr_tail)

    def test_oversized_and_incomplete_markers_reach_real_output(self):
        received, capture = self._collect(
            [self.OVERSIZED, b"npm \x1b]unterminated"]
        )
        text = _joined_text(received) + capture.stdout_tail
        self.assertIn(OVERSIZED_CONTROL_SEQUENCE, text)
        self.assertIn(INCOMPLETE_CONTROL_SEQUENCE, text)
        self.assertNotIn("\x1b", text)

    def test_real_collector_line_bound_marker_and_recovery(self):
        oversized_line = (
            b"line " * (DIAGNOSTIC_LINE_LIMIT_BYTES // 5) + b"discard-me" * 100 + b"\n"
        )
        received, _capture = self._collect([oversized_line, b"next line\n"])
        texts = [chunk.text for chunk in received]
        joined = "\n".join(texts)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, joined)
        self.assertIn("next line", texts)
        self.assertNotIn("discard-me", joined)
        self._assert_terminal_safe(self, joined)

    def test_real_collector_through_interactive_and_lines_presentation(self):
        for mode in (
            HostPresentationMode.INTERACTIVE,
            HostPresentationMode.LINES,
        ):
            with self.subTest(mode=mode):
                received, _capture = self._collect([self.RAW, self.OVERSIZED])
                renderer = _TextRecordingRenderer()
                state = HostPresentationState(renderer, mode=mode)
                # Complete-line consumers see only finalized record boundaries;
                # a committed prefix is never admitted as a diagnostic.
                self.assertTrue(any(not c.finalized for c in received))
                for chunk in received:
                    if not chunk.finalized:
                        continue
                    state.admit_diagnostic(
                        HostStructuredDiagnostic(
                            phase=HostPhase.LOCKED_ASSEMBLY,
                            step=HostStep.NPM_EXECUTION,
                            stream=HostDiagnosticStream(chunk.stream),
                            classification=classify_npm_diagnostic(chunk.text),
                            text=chunk.text,
                            hostnames=chunk.hostnames,
                            url_fingerprints=chunk.url_fingerprints,
                        ),
                        now=0.0,
                    )
                state.finalize_group()
                rendered = "\n".join(renderer.texts)
                self.assertTrue(renderer.texts)
                self._assert_terminal_safe(self, rendered)
                self.assertIn(r"\x1b[31m", rendered)

    def test_real_collector_text_failure_report_in_every_mode(self):
        _received, capture = self._collect([self.RAW, self.OVERSIZED])
        error = _exit_failure(
            1,
            stderr=capture.stderr_tail,
            stdout=capture.stdout_tail,
            truncation_notice=capture.truncation_notice,
        )
        context = _host_failure_context(error)
        self.assertTrue(context.tail)
        for display in NetworkUrlDisplay:
            with self.subTest(display=display):
                report = format_failure_report(
                    context.phase,
                    context.step,
                    summary=context.summary,
                    tail=context.tail,
                    tail_stream=context.tail_stream,
                    timeout_retained_context=context.timeout_retained_context,
                    logical_resource=context.logical_resource,
                    hostnames=context.hostnames,
                    network_url_display=display,
                    exception_types=context.exception_types,
                )
                self._assert_terminal_safe(self, report)

    def test_real_json_failure_document_tail_comes_from_collector(self):
        _received, capture = self._collect([self.RAW, self.OVERSIZED])
        collector_tail = (capture.stderr_tail or capture.stdout_tail).strip()
        error = _exit_failure(
            1,
            stderr=capture.stderr_tail,
            stdout=capture.stdout_tail,
            truncation_notice=capture.truncation_notice,
        )
        result = BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=str(error),
            host_failure=_host_failure_context(error),
        )
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="json",
            verbose=False,
            color="never",
            command_args={"yes": True},
        )
        with mock.patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=result,
        ):
            converted = _real_dispatcher("build", request)
        stdout, stderr = _render(
            "build",
            converted,
            fmt="json",
            color="never",
            verbose=False,
            stdout_is_tty=False,
            stderr_is_tty=False,
        )
        self.assertEqual("", stderr)
        # Exactly one valid JSON document.
        payload = json.loads(stdout)
        tail = payload["data"]["host_failure"]["tail"]
        self.assertEqual(collector_tail, tail)
        self.assertTrue(tail)
        self._assert_terminal_safe(self, stdout)

    def test_redacting_stream_route_is_terminal_safe(self):
        stream = RedactingStream(())
        out = "".join(stream.feed_bytes(self.RAW))
        out += "".join(stream.finish())
        self._assert_terminal_safe(self, out)
        self.assertIn(r"\x1b[31m", out)
        self._assert_terminal_safe(self, stream.tail())

    def test_default_collector_raw_controls_are_terminal_safe(self):
        received, capture = self._collect_default([self.RAW])
        self.assertTrue(received)
        for chunk in received:
            self._assert_terminal_safe(self, chunk.text)
        self._assert_terminal_safe(self, capture.stdout_tail)
        self._assert_terminal_safe(self, capture.stderr_tail)

    def test_default_collector_oversized_control_and_line_markers(self):
        oversized_line = (
            b"head " * (DIAGNOSTIC_LINE_LIMIT_BYTES // 5)
            + b"discard-me" * 100
            + b"\n"
        )
        received, capture = self._collect_default(
            [self.OVERSIZED, oversized_line, b"recovered line\n"]
        )
        text = _joined_text(received)
        self.assertIn(OVERSIZED_CONTROL_SEQUENCE, text)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, text)
        self.assertIn("recovered line", text)
        # The committed prefix is delivered; only the suffix is discarded.
        self.assertIn("head head", text)
        self.assertNotIn("discard-me", text)
        self._assert_terminal_safe(self, text)
        self._assert_terminal_safe(self, capture.stdout_tail)

    def test_default_collector_text_failure_report_is_terminal_safe(self):
        _received, capture = self._collect_default([self.RAW, self.OVERSIZED])
        error = _exit_failure(
            1,
            stderr=capture.stderr_tail,
            stdout=capture.stdout_tail,
            truncation_notice=capture.truncation_notice,
        )
        context = _host_failure_context(error)
        self.assertTrue(context.tail)
        for display in NetworkUrlDisplay:
            with self.subTest(display=display):
                report = format_failure_report(
                    context.phase,
                    context.step,
                    summary=context.summary,
                    tail=context.tail,
                    tail_stream=context.tail_stream,
                    timeout_retained_context=context.timeout_retained_context,
                    logical_resource=context.logical_resource,
                    hostnames=context.hostnames,
                    network_url_display=display,
                    exception_types=context.exception_types,
                )
                self._assert_terminal_safe(self, report)

    def test_default_collector_json_failure_tail_is_bounded_and_safe(self):
        _received, capture = self._collect_default([self.RAW, self.OVERSIZED])
        error = _exit_failure(
            1,
            stderr=capture.stderr_tail,
            stdout=capture.stdout_tail,
            truncation_notice=capture.truncation_notice,
        )
        result = BuildResult(
            exit_kind=ExitKind.OPERATIONAL,
            message=str(error),
            host_failure=_host_failure_context(error),
        )
        request = CommandRequest(
            command="build",
            constructor_project=ConstructorProject(Path.cwd().resolve()),
            output="json",
            verbose=False,
            color="never",
            command_args={"yes": True},
        )
        with mock.patch(
            "docker.versioning.build_orchestration.orchestrate_build",
            return_value=result,
        ):
            converted = _real_dispatcher("build", request)
        stdout, stderr = _render(
            "build",
            converted,
            fmt="json",
            color="never",
            verbose=False,
            stdout_is_tty=False,
            stderr_is_tty=False,
        )
        self.assertEqual("", stderr)
        payload = json.loads(stdout)
        tail = payload["data"]["host_failure"]["tail"]
        self.assertTrue(tail)
        self.assertLessEqual(len(tail.encode("utf-8")), TAIL_BYTES)
        self._assert_terminal_safe(self, stdout)

    def test_project_tail_route_is_terminal_safe_and_bounded(self):
        tail = project_tail(self.RAW.decode("utf-8"), tail_bytes=32)
        self._assert_terminal_safe(self, tail)
        self.assertLessEqual(len(tail.encode("utf-8")), 32)


class TestCollectStreams(unittest.TestCase):
    def test_concurrent_streams_reach_sink_before_exit(self):
        received: list[StreamChunk] = []
        received_event = threading.Event()

        def sink(chunk: StreamChunk) -> None:
            received.append(chunk)
            received_event.set()

        harness = _CollectHarness(sink=sink)
        thread = threading.Thread(target=harness.run)
        thread.start()
        try:
            # No newline: committed safe-prefix delivery must not wait for one.
            harness.write_stdout(b"stdout-no-newline")
            self.assertTrue(received_event.wait(2.0))
            self.assertTrue(any(c.stream == STREAM_STDOUT for c in received))
        finally:
            harness.close_writes()
            thread.join(2.0)
            harness.close_reads()
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(harness.capture)
        self.assertEqual(harness.capture.stdout_tail, "stdout-no-newline")

    def test_structured_prefix_reaches_sink_before_newline(self):
        received: list[StreamChunk] = []
        fed = threading.Event()
        release = threading.Event()

        def read_stdout(n: int) -> bytes:
            if not fed.is_set():
                fed.set()
                return b"npm warn no newline here "
            release.wait(5.0)
            return b""

        def read_stderr(n: int) -> bytes:
            release.wait(5.0)
            return b""

        def stop() -> None:
            self.assertTrue(fed.wait(5.0))
            release.set()

        stopper = threading.Thread(target=stop, daemon=True)
        stopper.start()
        capture = collect_streams(
            stdout_read=read_stdout,
            stderr_read=read_stderr,
            sink=received.append,
            stream_factory=make_stream_factory(),
            tail_projector=project_tail,
        )
        stopper.join(5.0)
        self.assertTrue(received)
        prefix_text = "".join(
            chunk.text for chunk in received if not chunk.finalized
        )
        self.assertIn("npm warn no newline here", prefix_text)
        self.assertIn("npm warn no newline here", capture.stdout_tail)
        # The complete line is finalized separately and never rewrites the tail.
        self.assertEqual(
            capture.stdout_tail, "npm warn no newline here "
        )

    def test_serialized_sink_invoked_on_one_thread(self):
        ids: list[int] = []
        lock = threading.Lock()

        def sink(chunk: StreamChunk) -> None:
            with lock:
                ids.append(threading.get_ident())

        harness = _CollectHarness(sink=sink)
        thread = threading.Thread(target=harness.run)
        thread.start()
        try:
            harness.write_stdout(b"a" * 1000)
            harness.write_stderr(b"b" * 1000)
        finally:
            harness.close_writes()
            thread.join(2.0)
            harness.close_reads()
        self.assertFalse(thread.is_alive())
        self.assertGreater(len(ids), 1)
        self.assertEqual(len(set(ids)), 1)

    def test_reads_are_bounded_to_chunk_size(self):
        requested: list[int] = []

        def read_stdout(n: int) -> bytes:
            requested.append(n)
            return b""

        def read_stderr(n: int) -> bytes:
            requested.append(n)
            return b""

        capture = collect_streams(
            stdout_read=read_stdout, stderr_read=read_stderr
        )
        self.assertTrue(requested)
        self.assertTrue(all(n <= READ_CHUNK_BYTES for n in requested))
        self.assertEqual(capture.stdout_tail, "")
        self.assertEqual(capture.truncation_notice, None)

    def test_default_path_oversized_line_marker_recovery_and_bounded_tail(self):
        received: list[StreamChunk] = []
        prefix = b"Q" * DIAGNOSTIC_LINE_LIMIT_BYTES
        oversized = prefix + b"S" * 100 + b"\n"
        capture = collect_streams(
            stdout_read=_ScriptedReader([oversized, b"valid line\n"]),
            stderr_read=_ScriptedReader([]),
            sink=received.append,
        )
        texts = [chunk.text for chunk in received]
        joined = "".join(texts)
        # Exactly one overflow marker after the committed prefix, no discarded
        # suffix, and recovery on the next line.
        self.assertEqual(joined.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertIn("Q", joined)
        self.assertNotIn("S", joined)
        self.assertIn("valid line", joined)
        self.assertLessEqual(len(capture.stdout_tail.encode("utf-8")), TAIL_BYTES)
        self.assertNotIn("S", capture.stdout_tail)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, capture.stdout_tail)

    def test_no_sink_produces_no_live_output_and_bounded_tail(self):
        harness = _CollectHarness(sink=None, secrets=("SECRET",))
        thread = threading.Thread(target=harness.run)
        thread.start()
        try:
            harness.write_stdout(b"SECRET and more")
        finally:
            harness.close_writes()
            thread.join(2.0)
            harness.close_reads()
        self.assertIsNotNone(harness.capture)
        self.assertNotIn("SECRET", harness.capture.stdout_tail)
        self.assertIsNone(harness.capture.truncation_notice)


class TestFacadeDirectEnqueuePath(unittest.TestCase):
    def test_lookalike_attributes_cannot_bypass_dispatcher(self):
        for attribute in (
            "internal_direct_enqueue",
            "_constructor_direct_enqueue",
        ):
            with self.subTest(attribute=attribute):
                threads: list[str] = []

                def sink(chunk: StreamChunk) -> None:
                    threads.append(threading.current_thread().name)

                setattr(sink, attribute, True)
                stdout = iter((b"output", b""))
                stderr = iter((b"",))
                capture = collect_streams(
                    stdout_read=lambda size: next(stdout),
                    stderr_read=lambda size: next(stderr),
                    sink=sink,
                )
                self.assertEqual("output", capture.stdout_tail)
                self.assertEqual(["npm-sink-dispatcher"], threads)

    def test_throwing_lookalike_callback_remains_isolated(self):
        def sink(chunk: StreamChunk) -> None:
            raise RuntimeError("external callback failed")

        sink.internal_direct_enqueue = True
        sink._constructor_direct_enqueue = True
        stdout = iter((b"safe output", b""))
        stderr = iter((b"",))
        capture = collect_streams(
            stdout_read=lambda size: next(stdout),
            stderr_read=lambda size: next(stderr),
            sink=sink,
        )
        self.assertEqual("safe output", capture.stdout_tail)

    def test_blocking_lookalike_does_not_block_reader_threads(self):
        callback_entered = threading.Event()
        release_callback = threading.Event()
        readers_finished = threading.Event()
        eof_count = 0
        eof_lock = threading.Lock()

        def sink(chunk: StreamChunk) -> None:
            callback_entered.set()
            release_callback.wait(2)

        sink.internal_direct_enqueue = True
        stdout = iter((b"output", b""))
        stderr = iter((b"",))

        def read(source, size):
            nonlocal eof_count
            value = next(source)
            if not value:
                with eof_lock:
                    eof_count += 1
                    if eof_count == 2:
                        readers_finished.set()
            return value

        thread = threading.Thread(
            target=lambda: collect_streams(
                stdout_read=lambda size: read(stdout, size),
                stderr_read=lambda size: read(stderr, size),
                sink=sink,
            )
        )
        thread.start()
        try:
            self.assertTrue(callback_entered.wait(1))
            self.assertTrue(readers_finished.wait(1))
        finally:
            release_callback.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_arbitrary_callback_retains_dispatcher_isolation(self):
        threads: list[str] = []
        stdout = iter((b"output", b""))
        stderr = iter((b"",))
        collect_streams(
            stdout_read=lambda size: next(stdout),
            stderr_read=lambda size: next(stderr),
            sink=lambda chunk: threads.append(threading.current_thread().name),
        )
        self.assertEqual(["npm-sink-dispatcher"], threads)


class TestSinkDispatcher(unittest.TestCase):
    def test_slow_compliant_sink_delivers_all_in_order(self):
        delivered: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            time.sleep(0.005)
            delivered.append(chunk.text)

        dispatcher = SinkDispatcher(sink)
        chunks = [StreamChunk(STREAM_STDOUT, f"c{i}") for i in range(20)]
        for chunk in chunks:
            self.assertTrue(dispatcher.submit(chunk))
        self.assertIsNone(dispatcher.finish())
        self.assertEqual(delivered, [c.text for c in chunks])
        self.assertFalse(dispatcher.is_alive())

    def test_queue_overflow_drops_only_excess_with_one_notice(self):
        gate = threading.Event()
        delivered: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            gate.wait()
            delivered.append(chunk.text)

        dispatcher = SinkDispatcher(sink)
        accepted = 0
        for i in range(200):
            if dispatcher.submit(StreamChunk(STREAM_STDOUT, str(i))):
                accepted += 1
        self.assertLessEqual(accepted, SINK_QUEUE_CAPACITY + 1)
        self.assertIsNotNone(dispatcher.truncation_notice())
        gate.set()
        notice = dispatcher.finish()
        self.assertEqual(notice, TRUNCATION_NOTICE)
        self.assertFalse(dispatcher.is_alive())
        # Delivered chunks are a contiguous accepted-order prefix.
        self.assertEqual(
            delivered, [str(i) for i in range(len(delivered))]
        )

    def test_raising_sink_disables_delivery_with_one_notice(self):
        delivered: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk.text)
            raise RuntimeError("sink failed")

        dispatcher = SinkDispatcher(sink)
        self.assertTrue(dispatcher.submit(StreamChunk(STREAM_STDOUT, "a")))
        self.assertTrue(dispatcher.submit(StreamChunk(STREAM_STDOUT, "b")))
        notice = dispatcher.finish()
        self.assertEqual(notice, TRUNCATION_NOTICE)
        self.assertEqual(delivered, ["a"])
        self.assertFalse(dispatcher.is_alive())

    def test_normal_finalization_drains_all_and_joins(self):
        state = {"returned": False, "calls": []}

        def sink(chunk: StreamChunk) -> None:
            state["calls"].append((state["returned"], chunk))

        harness = _CollectHarness(sink=sink)
        thread = threading.Thread(target=harness.run)
        thread.start()
        try:
            harness.write_stdout(b"line1\nline2")
            harness.write_stderr(b"err1")
        finally:
            harness.close_writes()
            thread.join(2.0)
            harness.close_reads()
        state["returned"] = True
        self.assertIsNotNone(harness.capture)
        # No callback fired after executor return.
        self.assertTrue(all(not returned for returned, _ in state["calls"]))
        self.assertIsNone(harness.capture.truncation_notice)
        self.assertEqual(harness.capture.stdout_tail, "line1\nline2")
        self.assertEqual(harness.capture.stderr_tail, "err1")

    def test_callback_over_100ms_budget_fails(self):
        delivered: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            time.sleep(0.15)
            delivered.append(chunk.text)

        dispatcher = SinkDispatcher(
            sink, callback_budget=0.05, drain_budget=2.0
        )
        self.assertTrue(dispatcher.submit(StreamChunk(STREAM_STDOUT, "a")))
        self.assertTrue(dispatcher.submit(StreamChunk(STREAM_STDOUT, "b")))
        notice = dispatcher.finish()
        self.assertEqual(notice, TRUNCATION_NOTICE)
        self.assertEqual(delivered, ["a"])
        self.assertFalse(dispatcher.is_alive())

    def test_drain_budget_exhaustion_discards_and_notices(self):
        delivered: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            time.sleep(0.05)
            delivered.append(chunk.text)

        # A compliant sink (50 ms << 1 s callback budget) but a tight drain
        # budget: draining 64 chunks at 50 ms each exceeds 0.2 s.
        dispatcher = SinkDispatcher(
            sink, callback_budget=1.0, drain_budget=0.2
        )
        for i in range(SINK_QUEUE_CAPACITY):
            self.assertTrue(
                dispatcher.submit(StreamChunk(STREAM_STDOUT, str(i)))
            )
        notice = dispatcher.finish()
        self.assertEqual(notice, TRUNCATION_NOTICE)
        self.assertFalse(dispatcher.is_alive())
        self.assertGreater(len(delivered), 0)
        self.assertLess(len(delivered), SINK_QUEUE_CAPACITY)

    def test_overflow_loss_records_exactly_one_notice(self):
        gate = threading.Event()

        def sink(chunk: StreamChunk) -> None:
            gate.wait()

        dispatcher = SinkDispatcher(sink)
        for i in range(200):
            dispatcher.submit(StreamChunk(STREAM_STDOUT, str(i)))
        gate.set()
        dispatcher.finish()
        # The notice is a single fixed literal, regardless of drop count.
        self.assertEqual(dispatcher.truncation_notice(), TRUNCATION_NOTICE)

    def test_submit_after_acceptance_closes_returns_false(self):
        delivered: list[str] = []
        dispatcher = SinkDispatcher(lambda c: delivered.append(c.text))
        self.assertIsNone(dispatcher.finish())
        # Acceptance is closed: the submission is refused, not enqueued.
        self.assertFalse(
            dispatcher.submit(StreamChunk(STREAM_STDOUT, "late"))
        )
        self.assertNotIn("late", delivered)
        self.assertEqual(dispatcher.truncation_notice(), TRUNCATION_NOTICE)

    def test_submit_acceptance_and_enqueue_are_atomic_vs_finish(self):
        delivered: list[str] = []
        q = _CoordinatedQueue(maxsize=8)

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk.text)

        dispatcher = SinkDispatcher(
            sink, queue_capacity=8, queue_factory=lambda n: q
        )
        result: dict[str, bool] = {}

        def do_submit() -> None:
            result["accepted"] = dispatcher.submit(
                StreamChunk(STREAM_STDOUT, "race")
            )

        submit_thread = threading.Thread(target=do_submit)
        submit_thread.start()
        # 1. submit() has passed the acceptance decision and is paused inside
        #    the atomic check+enqueue critical section.
        self.assertTrue(q.inside_put.wait(2.0))

        # 2. finish() attempts to close acceptance and enqueue the sentinel;
        #    it must not interleave with the in-flight submit.
        finish_result: dict[str, str | None] = {}

        def do_finish() -> None:
            finish_result["notice"] = dispatcher.finish()

        finish_thread = threading.Thread(target=do_finish)
        finish_thread.start()

        # 3. The submission completes.
        q.allow_put.set()
        submit_thread.join(2.0)
        self.assertFalse(submit_thread.is_alive())

        # 4. Finalization completes.
        finish_thread.join(2.0)
        self.assertFalse(finish_thread.is_alive())

        # The submission was accepted and its chunk was enqueued before the
        # sentinel, then delivered exactly once before finish() returned.
        self.assertTrue(result["accepted"])
        self.assertEqual(delivered, ["race"])
        self.assertIsNone(finish_result["notice"])
        self.assertLess(
            q.enqueue_log.index(("chunk", "race")),
            q.enqueue_log.index(("sentinel",)),
        )
        self.assertFalse(dispatcher.is_alive())

    def test_concurrent_submitters_race_finalization(self):
        rounds = 100
        per_round = 32
        for round_index in range(rounds):
            delivered: list[str] = []
            finish_returned = threading.Event()
            callbacks_after_finish: list[str] = []

            def sink(chunk: StreamChunk) -> None:
                if finish_returned.is_set():
                    callbacks_after_finish.append(chunk.text)
                delivered.append(chunk.text)

            dispatcher = SinkDispatcher(sink, queue_capacity=8)
            accepted = [False] * per_round

            def submit_one(i: int) -> None:
                accepted[i] = dispatcher.submit(
                    StreamChunk(STREAM_STDOUT, f"r{round_index}-c{i}")
                )

            def do_finish() -> None:
                dispatcher.finish()
                finish_returned.set()

            submitters = [
                threading.Thread(target=submit_one, args=(i,))
                for i in range(per_round)
            ]
            finisher = threading.Thread(target=do_finish)
            for t in submitters:
                t.start()
            finisher.start()
            for t in submitters:
                t.join(2.0)
            finisher.join(2.0)
            self.assertFalse(finisher.is_alive())

            # Every successfully accepted chunk is delivered exactly once
            # during normal finalization; no rejected chunk is delivered.
            self.assertEqual(len(delivered), sum(accepted))
            self.assertEqual(len(delivered), len(set(delivered)))
            for i, ok in enumerate(accepted):
                label = f"r{round_index}-c{i}"
                if ok:
                    self.assertIn(label, delivered)
                else:
                    self.assertNotIn(label, delivered)
            # No callback executes after finish() returns.
            self.assertEqual(callbacks_after_finish, [])
            # The dispatcher thread terminates.
            self.assertFalse(dispatcher.is_alive())
            # Queue storage never exceeds its configured capacity.
            self.assertLessEqual(dispatcher._queue.qsize(), 8)


def _assert_no_workers(testcase: unittest.TestCase) -> None:
    live = {t.name for t in threading.enumerate()}
    for name in (
        "npm-stdout-reader",
        "npm-stderr-reader",
        "npm-sink-dispatcher",
    ):
        testcase.assertNotIn(name, live)


class _FailingPipe:
    """Pipe stub whose reads raise, simulating a stream reader failure."""

    def read(self, n: int) -> bytes:
        raise OSError("stdout reader failure")

    def close(self) -> None:
        pass


class _BlockedPipe:
    """Pipe stub that blocks on read until unblocked (terminate or close)."""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data
        self._terminated = threading.Event()

    def read(self, n: int) -> bytes:
        self._terminated.wait()
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk

    def close(self) -> None:
        self._terminated.set()

    def unblock(self) -> None:
        self._terminated.set()


class _FakeStreamingProc:
    """``subprocess.Popen`` stub: failing stdout, blocked stderr."""

    def __init__(self, stderr_data: bytes = b"") -> None:
        self.stdout = _FailingPipe()
        self.stderr = _BlockedPipe(stderr_data)
        self.returncode: int | None = None
        self.reaped = False

    def terminate(self) -> None:
        self.stderr.unblock()

    def kill(self) -> None:
        self.stderr.unblock()

    def wait(self, timeout: float | None = None) -> int:
        self.reaped = True
        self.returncode = -15
        return self.returncode


class _CloseTrackingPipe:
    """Pipe stub tracking close attempts; read fails, blocks, or returns EOF."""

    def __init__(
        self,
        *,
        fail_read: bool = False,
        fail_close: bool = False,
        block: bool = False,
        close_error: str = "SUPERSECRET close failed",
    ) -> None:
        self.close_attempts = 0
        self._fail_read = fail_read
        self._fail_close = fail_close
        self._block = block
        self._close_error = close_error
        self._terminated = threading.Event()

    def read(self, n: int) -> bytes:
        if self._fail_read:
            raise OSError("stdout reader failure")
        if self._block:
            self._terminated.wait()
        return b""

    def close(self) -> None:
        self.close_attempts += 1
        if self._fail_close:
            raise OSError(self._close_error)

    def unblock(self) -> None:
        self._terminated.set()


class _CloseTrackingProc:
    """``subprocess.Popen`` stub pairing two ``_CloseTrackingPipe``s."""

    def __init__(self, stdout_pipe, stderr_pipe) -> None:
        self.stdout = stdout_pipe
        self.stderr = stderr_pipe
        self.returncode: int | None = None
        self.reaped = False

    def terminate(self) -> None:
        self.stderr.unblock()

    def kill(self) -> None:
        self.stderr.unblock()

    def wait(self, timeout: float | None = None) -> int:
        self.reaped = True
        self.returncode = -15
        return self.returncode


class TestReaderFailure(unittest.TestCase):
    def _cache_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-reader-fail-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cache"
        root.mkdir()
        return root

    def test_stdout_reader_failure_continues_stderr_and_raises(self):
        delivered: list[StreamChunk] = []

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk)

        stderr_data = iter([b"stderr line\n"])

        def read_stderr(n: int) -> bytes:
            return next(stderr_data, b"")

        def fail_stdout(n: int) -> bytes:
            raise OSError("stdout reader died")

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=fail_stdout,
                stderr_read=read_stderr,
                sink=sink,
            )
        exc = ctx.exception
        self.assertEqual(exc.stream, STREAM_STDOUT)
        self.assertEqual(exc.truncation_notice, TRUNCATION_NOTICE)
        # Stderr kept draining and reached the sink through the dispatcher.
        self.assertTrue(any(c.stream == STREAM_STDERR for c in delivered))
        self.assertFalse(any(c.stream == STREAM_STDOUT for c in delivered))
        _assert_no_workers(self)

    def test_stderr_reader_failure_continues_stdout_and_raises(self):
        delivered: list[StreamChunk] = []

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk)

        stdout_data = iter([b"stdout line\n"])

        def read_stdout(n: int) -> bytes:
            return next(stdout_data, b"")

        def fail_stderr(n: int) -> bytes:
            raise OSError("stderr reader died")

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=read_stdout,
                stderr_read=fail_stderr,
                sink=sink,
            )
        exc = ctx.exception
        self.assertEqual(exc.stream, STREAM_STDERR)
        self.assertEqual(exc.truncation_notice, TRUNCATION_NOTICE)
        self.assertTrue(any(c.stream == STREAM_STDOUT for c in delivered))
        self.assertFalse(any(c.stream == STREAM_STDERR for c in delivered))
        _assert_no_workers(self)

    def test_split_secret_reader_failure_does_not_emit_pending_prefix(self):
        received: list[str] = []

        def sink(chunk: StreamChunk) -> None:
            received.append(chunk.text)

        state = {"first": True}

        def fail_stdout(n: int) -> bytes:
            if state["first"]:
                state["first"] = False
                return b"prefix SUPERS"
            raise OSError("reader died")

        def read_stderr(n: int) -> bytes:
            return b""

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=fail_stdout,
                stderr_read=read_stderr,
                secrets=("SUPERSECRET",),
                sink=sink,
            )
        joined = "".join(received)
        # The pending secret prefix must be redacted, never emitted, during
        # the failure flush.
        self.assertNotIn("SUPERS", joined)
        self.assertIn(REDACTED, joined)
        self.assertEqual(ctx.exception.stream, STREAM_STDOUT)

    def test_reader_failure_detail_is_bounded_and_redacted(self):
        secret = "S3CRET"
        huge = f"{secret}{'x' * (READER_FAILURE_DETAIL_BYTES * 2)}{secret}"

        def fail(n: int) -> bytes:
            raise RuntimeError(huge)

        def read_stderr(n: int) -> bytes:
            return b""

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=fail,
                stderr_read=read_stderr,
                secrets=(secret,),
            )
        detail = ctx.exception.detail
        self.assertNotIn(secret, detail)
        self.assertLessEqual(
            len(detail.encode("utf-8")), READER_FAILURE_DETAIL_BYTES
        )

    def test_run_streaming_reader_failure_unblocks_blocked_sibling(self):
        import docker.npm_environment.execution as execution_module

        fake = _FakeStreamingProc(stderr_data=b"remaining stderr\n")
        delivered: list[StreamChunk] = []

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk)

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"), sink=sink
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=fake
        ):
            thread = threading.Thread(target=run, name="test-run-streaming")
            thread.start()
            thread.join(5.0)
        self.assertFalse(
            thread.is_alive(), "run_streaming hung on a blocked sibling reader"
        )

        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        assert isinstance(exc, StreamReaderFailure)
        self.assertEqual(exc.stream, STREAM_STDOUT)
        self.assertTrue(fake.reaped, "local client was not terminated and reaped")
        self.assertEqual(fake.returncode, -15)
        # The sibling reader drained its remaining EOF-safe data after the
        # client was terminated.
        self.assertTrue(any(c.stream == STREAM_STDERR for c in delivered))
        _assert_no_workers(self)

    def test_terminate_and_reap_reaps_real_process(self):
        import docker.npm_environment.execution as execution_module

        proc = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(30)")
        )
        try:
            execution_module._terminate_and_reap(proc, grace_seconds=5.0)
            with self.assertRaises(ProcessLookupError):
                os.kill(proc.pid, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_collect_streams_callback_failure_is_secondary_context(self):
        unblock = threading.Event()
        remaining = [b"remaining stderr\n"]

        def stdout_read(n: int) -> bytes:
            raise OSError("stdout reader failure")

        def stderr_read(n: int) -> bytes:
            unblock.wait()
            if not remaining[0]:
                return b""
            data, remaining[0] = remaining[0][:n], remaining[0][n:]
            return data

        def on_reader_failure(stream: str) -> None:
            # Unblock the sibling reader, then fail the cleanup itself.
            unblock.set()
            raise RuntimeError("callback cleanup failed")

        delivered: list[StreamChunk] = []

        def sink(chunk: StreamChunk) -> None:
            delivered.append(chunk)

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=stdout_read,
                stderr_read=stderr_read,
                secrets=("SUPERSECRET",),
                sink=sink,
                on_reader_failure=on_reader_failure,
            )

        exc = ctx.exception
        self.assertEqual(exc.stream, STREAM_STDOUT)
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("on_reader_failure", notes)
        self.assertIn("RuntimeError", notes)
        self.assertIn("callback cleanup failed", notes)
        # The sibling reader still drained its EOF-safe data to the sink
        # before the structured failure surfaced.
        self.assertTrue(any(c.stream == STREAM_STDERR for c in delivered))
        _assert_no_workers(self)

    def test_reader_failure_cleanup_note_is_url_free(self):
        unblock = threading.Event()
        remaining = [b"remaining stderr\n"]

        def stdout_read(n: int) -> bytes:
            raise OSError("stdout reader failure")

        def stderr_read(n: int) -> bytes:
            unblock.wait(5.0)
            if not remaining[0]:
                return b""
            data, remaining[0] = remaining[0][:n], remaining[0][n:]
            return data

        def on_reader_failure(stream: str) -> None:
            # Unblock the sibling reader, then fail cleanup with a URL that
            # carries credentials, a query, and a fragment.
            unblock.set()
            raise RuntimeError(
                "cleanup failed fetching "
                "https://user:pass@registry.example.com/pkg?token=abc#frag"
            )

        with self.assertRaises(StreamReaderFailure) as ctx:
            collect_streams(
                stdout_read=stdout_read,
                stderr_read=stderr_read,
                secrets=("SUPERSECRET",),
                on_reader_failure=on_reader_failure,
                tail_projector=project_tail,
            )

        exc = ctx.exception
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        for fragment in (
            "registry.example.com",
            "https://",
            "user:pass",
            "token=",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, rendered)
        self.assertIn("on_reader_failure cleanup failed", rendered)
        _assert_no_workers(self)

    @unittest.skipUnless(
        hasattr(signal, "pthread_kill"), "requires signal.pthread_kill"
    )
    def test_interruption_cleanup_note_is_url_free(self):
        blocked = threading.Event()
        unblock = threading.Event()

        def stdout_read(n: int) -> bytes:
            blocked.set()
            unblock.wait(5.0)
            return b""

        def stderr_read(n: int) -> bytes:
            unblock.wait(5.0)
            return b""

        def sink(chunk: StreamChunk) -> None:
            pass

        def on_interruption() -> None:
            # Unblock the readers so the coordinator can join them, then fail
            # cleanup with a URL that carries credentials, a query, and a
            # fragment.
            unblock.set()
            raise RuntimeError(
                "cleanup failed fetching "
                "https://user:pass@registry.example.com/pkg?token=abc#frag"
            )

        def interrupt() -> None:
            self.assertTrue(blocked.wait(5.0))
            time.sleep(0.1)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with self.assertRaises(KeyboardInterrupt) as ctx:
            collect_streams(
                stdout_read=stdout_read,
                stderr_read=stderr_read,
                sink=sink,
                secrets=("SUPERSECRET",),
                on_interruption=on_interruption,
                tail_projector=project_tail,
            )
        timer.join(5.0)

        rendered = "\n".join(
            [str(ctx.exception), *getattr(ctx.exception, "__notes__", ())]
        )
        for fragment in (
            "registry.example.com",
            "https://",
            "user:pass",
            "token=",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, rendered)
        self.assertIn("interruption cleanup failed", rendered)
        _assert_no_workers(self)

    def test_run_streaming_close_failure_is_secondary_context(self):
        import docker.npm_environment.execution as execution_module

        stdout_pipe = _CloseTrackingPipe(fail_read=True)
        stderr_pipe = _CloseTrackingPipe(block=True, fail_close=True)
        proc = _CloseTrackingProc(stdout_pipe, stderr_pipe)

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            thread = threading.Thread(
                target=run, name="test-run-streaming-close"
            )
            thread.start()
            thread.join(5.0)
        self.assertFalse(
            thread.is_alive(), "run_streaming hung during pipe cleanup"
        )

        exc = result.get("error")
        self.assertIsInstance(exc, StreamReaderFailure)
        assert isinstance(exc, StreamReaderFailure)
        # The reader failure stays primary; the close failure is only
        # bounded, redacted secondary context.
        self.assertEqual(exc.stream, STREAM_STDOUT)
        self.assertIn("reader failure", exc.detail)
        self.assertNotIn("close failed", exc.detail)
        notes = "\n".join(getattr(exc, "__notes__", ()))
        self.assertIn("pipe close failed", notes)
        self.assertIn("OSError", notes)
        self.assertNotIn("SUPERSECRET", notes)
        self.assertIn("<redacted>", notes)
        # Both pipes received a close attempt even though one close raised.
        self.assertGreaterEqual(stdout_pipe.close_attempts, 1)
        self.assertGreaterEqual(stderr_pipe.close_attempts, 1)
        _assert_no_workers(self)

    def test_run_streaming_close_failure_reaps_before_raising(self):
        import docker.npm_environment.execution as execution_module

        # Both readers reach EOF normally; only stderr's close() raises.
        stdout_pipe = _CloseTrackingPipe()
        stderr_pipe = _CloseTrackingPipe(fail_close=True)
        proc = _CloseTrackingProc(stdout_pipe, stderr_pipe)

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            thread = threading.Thread(
                target=run, name="test-run-streaming-close-normal"
            )
            thread.start()
            thread.join(5.0)
        self.assertFalse(
            thread.is_alive(), "run_streaming hung during cleanup"
        )

        exc = result.get("error")
        # The deferred close failure surfaces, with the secret redacted.
        self.assertIsInstance(exc, OSError)
        self.assertIn("close failed", str(exc))
        self.assertNotIn("SUPERSECRET", str(exc))
        # The subprocess was still reaped before the close failure surfaced.
        self.assertTrue(proc.reaped)
        # Both pipes received a close attempt.
        self.assertGreaterEqual(stdout_pipe.close_attempts, 1)
        self.assertGreaterEqual(stderr_pipe.close_attempts, 1)
        _assert_no_workers(self)

    def test_run_streaming_reader_failure_close_note_is_url_free(self):
        import docker.npm_environment.execution as execution_module

        url = "https://user:pass@registry.example.com/pkg?token=abc#frag"
        # stdout fails to read, then fails to close with a URL; stderr blocks
        # until the client is terminated during reader-failure cleanup.
        stdout_pipe = _CloseTrackingPipe(
            fail_read=True,
            fail_close=True,
            close_error=f"close failed {url} SUPERSECRET",
        )
        stderr_pipe = _CloseTrackingPipe(block=True)
        proc = _CloseTrackingProc(stdout_pipe, stderr_pipe)

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                    tail_projector=project_tail,
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            thread = threading.Thread(
                target=run, name="test-run-streaming-reader-url"
            )
            thread.start()
            thread.join(5.0)
        self.assertFalse(
            thread.is_alive(), "run_streaming hung during cleanup"
        )

        exc = result.get("error")
        # The structured reader failure stays primary; the URL-bearing close
        # failure is only bounded secondary context.
        self.assertIsInstance(exc, StreamReaderFailure)
        assert isinstance(exc, StreamReaderFailure)
        rendered = "\n".join(
            [str(exc), exc.detail, *getattr(exc, "__notes__", ())]
        )
        self.assertIn("on_reader_failure cleanup failed", rendered)
        self.assertIn(REDACTED, rendered)
        for fragment in (
            url,
            "https://",
            "registry.example.com",
            "user:pass",
            "token=abc",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, rendered)
        _assert_no_workers(self)

    def test_run_streaming_close_failure_detail_is_url_free(self):
        import docker.npm_environment.execution as execution_module

        url = "https://user:pass@registry.example.com/pkg?token=abc#frag"
        # Both readers reach EOF normally; only stderr's close() raises a
        # credential-bearing URL.
        stdout_pipe = _CloseTrackingPipe()
        stderr_pipe = _CloseTrackingPipe(
            fail_close=True, close_error=f"close failed {url} SUPERSECRET"
        )
        proc = _CloseTrackingProc(stdout_pipe, stderr_pipe)

        result: dict = {}

        def run() -> None:
            try:
                DockerRunExecutor().run_streaming(
                    ("docker", "run", "--rm", "alpine", "true"),
                    secrets=("SUPERSECRET",),
                    tail_projector=project_tail,
                )
            except BaseException as exc:  # pragma: no cover - test aid
                result["error"] = exc

        with mock.patch.object(
            execution_module.subprocess, "Popen", return_value=proc
        ):
            thread = threading.Thread(
                target=run, name="test-run-streaming-close-url"
            )
            thread.start()
            thread.join(5.0)
        self.assertFalse(
            thread.is_alive(), "run_streaming hung during cleanup"
        )

        exc = result.get("error")
        self.assertIsInstance(exc, OSError)
        assert isinstance(exc, OSError)
        rendered = "\n".join([str(exc), *getattr(exc, "__notes__", ())])
        self.assertIn("close failed", rendered)
        self.assertIn(REDACTED, rendered)
        for fragment in (
            url,
            "https://",
            "registry.example.com",
            "user:pass",
            "token=abc",
            "#frag",
            "SUPERSECRET",
        ):
            self.assertNotIn(fragment, rendered)
        # The subprocess was still reaped before the close failure surfaced.
        self.assertTrue(proc.reaped)
        _assert_no_workers(self)

    def test_reader_failure_cannot_publish(self):
        executor = _FailingStreamingExecutor()
        cache_root = self._cache_root()
        assembler = _assembler()
        with self.assertRaises(LockedNpmError) as ctx:
            assemble_environment(
                validated=_validated(),
                assembler=assembler,
                cache_root=cache_root,
                executor=executor,
                secrets=("SUPERSECRET",),
            )
        self.assertEqual(ctx.exception.reason, "executor_failure")
        self.assertNotIn("SUPERSECRET", ctx.exception.detail)
        outputs = assembler_namespace_path(cache_root, assembler.digest) / "outputs"
        self.assertEqual(list(outputs.iterdir()), [])

    def test_executor_failure_detail_is_url_free(self):
        executor = _UrlLeakingStreamingExecutor()
        with self.assertRaises(LockedNpmError) as ctx:
            assemble_environment(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=self._cache_root(),
                executor=executor,
                secrets=("SUPERSECRET",),
                tail_projector=project_tail,
            )
        self.assertEqual(ctx.exception.reason, "executor_failure")
        detail = ctx.exception.detail
        self.assertNotIn("registry.example.com", detail)
        self.assertNotIn("SUPERSECRET", detail)
        self.assertIn(REDACTED, detail)


class _StreamingExecutor:
    """Injected executor that streams through ``collect_streams``."""

    def __init__(self, *, stdout=b"", stderr=b"", return_code=0):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        return ProcessResult(argv, 0, "", "")

    def run_streaming(
        self, argv: tuple[str, ...], *, secrets=(), sink=None
    ) -> ProcessResult:
        self.calls.append(argv)
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()

        def produce() -> None:
            os.write(out_w, self.stdout)
            os.close(out_w)
            os.write(err_w, self.stderr)
            os.close(err_w)

        producer = threading.Thread(target=produce)
        producer.start()
        capture = collect_streams(
            stdout_read=lambda n: os.read(out_r, n),
            stderr_read=lambda n: os.read(err_r, n),
            secrets=secrets,
            sink=sink,
        )
        producer.join()
        os.close(out_r)
        os.close(err_r)
        return ProcessResult(
            argv,
            self.return_code,
            capture.stdout_tail,
            capture.stderr_tail,
            truncation_notice=capture.truncation_notice,
        )


class _FailingStreamingExecutor:
    """Injected executor whose streaming path raises a reader failure."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        return ProcessResult(argv, 0, "", "")

    def run_streaming(
        self, argv: tuple[str, ...], *, secrets=(), sink=None
    ) -> ProcessResult:
        raise StreamReaderFailure(
            STREAM_STDOUT, "OSError", "SUPERSECRET leaked", TRUNCATION_NOTICE
        )


class _UrlLeakingStreamingExecutor:
    """Injected executor whose streaming path raises a URL-bearing failure."""

    def run(self, argv: tuple[str, ...]) -> ProcessResult:
        return ProcessResult(argv, 0, "", "")

    def run_streaming(
        self, argv: tuple[str, ...], *, secrets=(), sink=None
    ) -> ProcessResult:
        raise RuntimeError(
            "worker failed fetching "
            "https://registry.example.com/pkg?token=abc (secret SUPERSECRET)"
        )


class TestAssembleIntegration(unittest.TestCase):
    def _cache_root(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="npm-env-stream-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "cache"
        root.mkdir()
        return root

    def test_injected_executor_without_sink_remains_usable(self):
        executor = _StreamingExecutor(
            stdout=b"installed\n", stderr=b"warn\n"
        )
        result = assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=self._cache_root(),
            executor=executor,
        )
        self.assertEqual(result.stdout, "installed\n")
        self.assertEqual(result.stderr, "warn\n")
        self.assertIsNone(result.truncation_notice)

    def test_streaming_executor_with_sink_delivers_redacted_output(self):
        received: list[StreamChunk] = []

        def sink(chunk: StreamChunk) -> None:
            received.append(chunk)

        executor = _StreamingExecutor(
            stdout=b"built with SUPERSECRET\n", stderr=b""
        )
        result = assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=self._cache_root(),
            executor=executor,
            secrets=("SUPERSECRET",),
            sink=sink,
        )
        self.assertNotIn("SUPERSECRET", result.stdout)
        self.assertIn("<redacted>", result.stdout)
        self.assertTrue(any(STREAM_STDOUT in c.stream for c in received))
        self.assertTrue(any("SUPERSECRET" not in c.text for c in received))

    def test_streaming_executor_without_sink_produces_no_live_output(self):
        executor = _StreamingExecutor(stdout=b"quiet\n")
        result = assemble(
            validated=_validated(),
            assembler=_assembler(),
            cache_root=self._cache_root(),
            executor=executor,
        )
        self.assertEqual(result.stdout, "quiet\n")
        self.assertIsNone(result.truncation_notice)

    def test_streaming_nonzero_exit_keeps_bounded_redacted_diagnostics(self):
        executor = _StreamingExecutor(
            stdout=b"", stderr=b"boom SUPERSECRET\n", return_code=1
        )
        with self.assertRaises(Exception) as ctx:
            assemble(
                validated=_validated(),
                assembler=_assembler(),
                cache_root=self._cache_root(),
                executor=executor,
                secrets=("SUPERSECRET",),
            )
        self.assertNotIn("SUPERSECRET", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
