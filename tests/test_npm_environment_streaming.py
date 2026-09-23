"""Phase 2 — bounded redacted streaming executor (tasks 2.1–2.12).

The standalone npm assembler executor drains stdout/stderr concurrently,
decodes UTF-8 incrementally, redacts with a caller-order-independent
leftmost-longest matcher, retains bounded 64 KiB tails, and serializes a
single constructor-owned sink behind a non-blocking 64-chunk queue.  These
tests exercise the streaming primitives directly and through ``assemble``.
"""

from __future__ import annotations

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
from docker.versioning.npm_diagnostic_stream import project_tail

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
        # The incomplete leading byte is held by the decoder; "caf" (with no
        # secrets) is safe to emit immediately, and the completed "é" stays
        # intact across the split.
        self.assertEqual(stream.feed_bytes(b"caf\xc3"), ("caf",))
        chunks = stream.feed_bytes(b"\xa9 done")
        self.assertEqual("".join(chunks), "é done")

    def test_four_byte_character_split_across_reads(self):
        stream = RedactingStream(())
        # U+1F600 GRINNING FACE = F0 9F 98 80
        self.assertEqual(stream.feed_bytes(b"\xf0"), ())
        self.assertEqual(stream.feed_bytes(b"\x9f"), ())
        self.assertEqual(stream.feed_bytes(b"\x98"), ())
        chunks = stream.feed_bytes(b"\x80")
        self.assertEqual("".join(chunks), "😀")

    def test_eof_flushes_final_partial_without_newline(self):
        stream = RedactingStream(())
        stream.feed_bytes(b"no trailing newline")
        self.assertEqual("".join(stream.finish()), "")
        self.assertEqual(stream.tail(), "no trailing newline")


class TestRedactingStream(unittest.TestCase):
    def test_tail_bounded_to_64_kib(self):
        stream = RedactingStream(())
        big = b"x" * (TAIL_BYTES * 2)
        stream.feed_bytes(big)
        stream.finish()
        tail = stream.tail()
        self.assertLessEqual(len(tail.encode("utf-8")), TAIL_BYTES)
        # The tail is a suffix of the emitted stream.
        self.assertTrue(tail.endswith("xxxx"))
        self.assertEqual(tail, "x" * len(tail))

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
        self.assertEqual(stream.feed_bytes(b"ab"), ())
        self.assertEqual(stream.feed_bytes(b"c"), (REDACTED,))
        self.assertEqual(stream.finish(), ())

    def test_redact_text_matches_stream_for_whole_input(self):
        secrets = ("abc", "abcdef", "bcde", "SUPERSECRET")
        for text in ("abcdef", "xabcdey", "before SUPERSECRET after", "no-secret"):
            stream = RedactingStream(secrets)
            emitted = "".join(stream.feed_bytes(text.encode("utf-8")))
            emitted += "".join(stream.finish())
            self.assertEqual(emitted, redact_text(text, secrets), text)


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
            # No newline: safe-prefix delivery must not wait for one.
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
