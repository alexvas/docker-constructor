"""Phase 8 RED tests — locked-assembly npm diagnostic collection (8.5–8.7).

These bind the collector-branch, bounded-tail, and diagnostic-selection
deliverables:

* secret redaction and boundary-safe URL sanitization/host extraction happen
  before URL-free text enters either the structured live branch or the
  retained tail, even when sensitive tokens are split across many small
  decoder/input chunks;
* pending sanitizer state stays within 8 KiB and decoded diagnostic-line
  assembly stays within 64 KiB while input continues draining, with the fixed
  safe overflow markers, recovery after a safe boundary, and safe EOF,
  reader-failure, and cancellation finalization;
* the URL-free sanitized tail preserves original safe ordering/occurrences and
  fixed replacement markers without grouping, excludes discarded overflow
  fragments and normalized host metadata, keeps the existing byte bounds and
  truncation semantics, and is the only retained text;
* sanitized npm lines become closed structured diagnostics carrying normalized
  hosts and ephemeral ordered URL fingerprints only on the structured branch,
  without Constructor lifecycle classification, coalescing, or an
  assembler-owned presentation timer.
"""
from __future__ import annotations

import inspect
import io
import signal
import threading
import time
import unittest

from docker.npm_environment.streaming import (
    StreamChunk,
    StreamReaderFailure,
    TAIL_BYTES,
    collect_streams,
)
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostStructuredDiagnostic,
)
from docker.versioning.npm_diagnostic_stream import (
    DIAGNOSTIC_LINE_LIMIT_BYTES,
    NpmDiagnosticStream,
    OVERSIZED_DIAGNOSTIC_MARKER,
    classify_npm_diagnostic,
    make_stream_factory,
    project_tail,
)
from docker.versioning.diagnostic_projection import (
    INCOMPLETE_TOKEN_MARKER,
    OVERSIZED_TOKEN_MARKER,
)

_REDACTED = "<redacted>"


def _collect(chunks, *, secrets=(), fingerprinter=None, tail_bytes=TAIL_BYTES):
    """Feed *chunks* through one stream and return (chunks, tail)."""
    stream = NpmDiagnosticStream(
        "stdout", secrets, fingerprinter=fingerprinter, tail_bytes=tail_bytes
    )
    live: list[StreamChunk] = []
    for chunk in chunks:
        live.extend(stream.feed_bytes(chunk))
    live.extend(stream.finish())
    return live, stream.tail()


class TestCollectorBranch(unittest.TestCase):
    def test_url_is_removed_before_live_branch_and_tail(self):
        identity = SessionUrlIdentity()
        url = "https://user:secret@registry.example.com/pkg/-/pkg-1.0.0.tgz?q=1#frag"
        live, tail = _collect(
            [f"npm error network timeout at: {url}\n".encode()],
            fingerprinter=identity,
        )
        joined = "".join(chunk.text for chunk in live)
        self.assertNotIn("registry.example.com", joined)
        self.assertNotIn("secret", joined)
        self.assertIn(_REDACTED, joined)
        self.assertNotIn("registry.example.com", tail)
        self.assertNotIn("secret", tail)
        self.assertNotIn("/pkg/-/", tail)
        # The normalized host and an ephemeral fingerprint live only on the
        # structured branch.
        self.assertEqual(live[0].hostnames, ("registry.example.com",))
        self.assertEqual(len(live[0].url_fingerprints), 1)
        self.assertEqual(len(live[0].url_fingerprints[0]), 64)

    def test_split_credentials_across_many_small_chunks(self):
        url = "https://alice:s3cr3t@registry.example.com/private/pkg.tgz"
        data = f"npm error fetching {url} failed\n".encode()
        live: list[StreamChunk] = []
        stream = NpmDiagnosticStream("stderr", ())
        for index in range(len(data)):
            live.extend(stream.feed_bytes(data[index : index + 1]))
        live.extend(stream.finish())
        joined = "".join(chunk.text for chunk in live)
        self.assertNotIn("s3cr3t", joined)
        self.assertNotIn("alice", joined)
        self.assertNotIn("registry.example.com", joined)
        self.assertNotIn("s3cr3t", stream.tail())
        self.assertNotIn("registry.example.com", stream.tail())

    def test_percent_encoded_url_form(self):
        live, tail = _collect(
            [b"npm http fetch GET h%74tps%3A%2F%2Fregistry.example.com/a 200\n"]
        )
        joined = "".join(chunk.text for chunk in live)
        self.assertNotIn("registry.example.com", joined)
        self.assertIn(_REDACTED, joined)
        self.assertNotIn("registry.example.com", tail)

    def test_incomplete_prefix_at_eof_fails_closed(self):
        live, tail = _collect([b"npm error https://regist"])
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, joined)
        self.assertNotIn("regist", joined)
        self.assertNotIn("regist", tail)

    def test_reader_failure_finalizes_without_flushing_candidate(self):
        stream = NpmDiagnosticStream("stdout", ())
        live = list(stream.feed_bytes(b"npm error https://regist"))
        live.extend(stream.finish(abort=True))
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, joined)
        self.assertNotIn("regist", joined)
        self.assertNotIn("regist", stream.tail())

    def test_oversized_unterminated_url_stays_within_pending_limit(self):
        secret_url = "https://registry.example.com/" + "a" * (16 * 1024)
        stream = NpmDiagnosticStream("stdout", ())
        observed: list[int] = []
        live: list[StreamChunk] = []
        for index in range(0, len(secret_url), 256):
            live.extend(stream.feed_bytes(secret_url[index : index + 256].encode()))
            observed.append(stream.pending_sanitizer_bytes)
        live.extend(stream.finish())
        self.assertLessEqual(max(observed), 8 * 1024)
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(OVERSIZED_TOKEN_MARKER, joined)
        self.assertNotIn("a" * 100, joined)
        self.assertNotIn("a" * 100, stream.tail())

    def test_oversized_newline_free_diagnostic_stays_within_line_limit(self):
        stream = NpmDiagnosticStream("stdout", ())
        observed: list[int] = []
        live: list[StreamChunk] = []
        payload = b"npm notice " + b"word " * (20 * 1024)
        for index in range(0, len(payload), 1024):
            live.extend(stream.feed_bytes(payload[index : index + 1024]))
            observed.append(stream.pending_line_bytes)
        live.extend(stream.finish())
        self.assertLessEqual(max(observed), DIAGNOSTIC_LINE_LIMIT_BYTES)
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, joined)
        self.assertNotIn("word word word", joined)
        self.assertNotIn("word word word", stream.tail())

    def test_recovery_after_safe_boundary(self):
        payload = b"npm notice " + b"chunk " * (20 * 1024) + b"\nrecovered line\n"
        live, tail = _collect([payload])
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, joined)
        self.assertIn("recovered line", joined)
        self.assertIn("recovered line", tail)

    def test_input_keeps_draining_after_overflow(self):
        payload = b"npm notice " + b"drain " * (20 * 1024) + b"\nok\n"
        stream = NpmDiagnosticStream("stdout", ())
        produced = 0
        for index in range(0, len(payload), 1024):
            produced += len(stream.feed_bytes(payload[index : index + 1024]))
        produced += len(stream.finish())
        self.assertGreater(produced, 0)
        self.assertLessEqual(stream.pending_line_bytes, DIAGNOSTIC_LINE_LIMIT_BYTES)
        self.assertLessEqual(stream.pending_sanitizer_bytes, 8 * 1024)


class TestBoundedTail(unittest.TestCase):
    def test_tail_preserves_occurrences_without_grouping(self):
        payload = b"npm warn deprecated a@1.0.0\n" * 3
        live, tail = _collect([payload])
        self.assertEqual(tail.count("npm warn deprecated a@1.0.0"), 3)
        self.assertNotIn("repeated", tail)
        self.assertEqual(
            [chunk.text for chunk in live].count("npm warn deprecated a@1.0.0"), 3
        )

    def test_tail_excludes_host_metadata_and_fingerprints(self):
        identity = SessionUrlIdentity()
        live, tail = _collect(
            [b"npm warn proxy https://registry.example.com/x\n"],
            fingerprinter=identity,
        )
        self.assertNotIn("registry.example.com", tail)
        for chunk in live:
            for fingerprint in chunk.url_fingerprints:
                self.assertNotIn(fingerprint, tail)

    def test_tail_byte_bound_unchanged(self):
        payload = b"npm notice padding line\n" * 4096
        _, tail = _collect([payload])
        self.assertLessEqual(len(tail.encode("utf-8")), TAIL_BYTES)
        self.assertTrue(tail.endswith("npm notice padding line\n"))

    def test_tail_excludes_discarded_overflow_fragment(self):
        payload = b"npm notice " + b"frag " * (20 * 1024) + b"\nok\n"
        _, tail = _collect([payload])
        self.assertNotIn("frag frag", tail)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, tail)
        self.assertIn("ok", tail)

    def test_project_tail_is_url_free(self):
        projected = project_tail(
            "see https://user:pw@registry.example.com/path for details",
            (),
        )
        self.assertNotIn("registry.example.com", projected)
        self.assertNotIn("user:pw", projected)

    def test_collect_streams_without_sink_returns_url_free_tail(self):
        import io

        from docker.npm_environment.streaming import collect_streams

        def reader(data: bytes):
            buffer = io.BytesIO(data)
            return lambda size: buffer.read(size)

        capture = collect_streams(
            stdout_read=reader(
                b"npm error see https://registry.example.com/secret\n"
            ),
            stderr_read=reader(b""),
            stream_factory=make_stream_factory(()),
            tail_projector=project_tail,
        )
        self.assertNotIn("registry.example.com", capture.stdout_tail)
        self.assertIn(_REDACTED, capture.stdout_tail)


class TestDiagnosticSelection(unittest.TestCase):
    def test_closed_classification_members(self):
        self.assertIs(
            classify_npm_diagnostic("npm warn deprecated x@1.0.0"),
            HostDiagnosticClassification.WARNING,
        )
        self.assertIs(
            classify_npm_diagnostic("npm error code E404"),
            HostDiagnosticClassification.ERROR,
        )
        self.assertIs(
            classify_npm_diagnostic(
                "npm http fetch GET <redacted> attempt 1 failed with 503"
            ),
            HostDiagnosticClassification.RETRY,
        )
        self.assertIs(
            classify_npm_diagnostic("npm error network timeout at: <redacted>"),
            HostDiagnosticClassification.TIMEOUT,
        )
        self.assertIs(
            classify_npm_diagnostic("npm http fetch GET 200 <redacted> (cache miss)"),
            HostDiagnosticClassification.STATUS,
        )

    def test_no_coalescing_in_collector(self):
        live, _ = _collect([b"npm warn same line\nnpm warn same line\n"])
        self.assertEqual([chunk.text for chunk in live].count("npm warn same line"), 2)
        self.assertTrue(all("repeated" not in chunk.text for chunk in live))

    def test_structured_branch_carries_hosts_and_fingerprints_only(self):
        identity = SessionUrlIdentity()
        live, tail = _collect(
            [b"npm warn proxy https://registry.example.com/path?token=abc\n"],
            fingerprinter=identity,
        )
        chunk = live[0]
        self.assertEqual(chunk.hostnames, ("registry.example.com",))
        self.assertEqual(len(chunk.url_fingerprints), 1)
        self.assertNotIn("registry.example.com", chunk.text)
        self.assertNotIn(chunk.url_fingerprints[0], tail)

    def test_host_facts_attach_to_their_own_line(self):
        identity = SessionUrlIdentity()
        live, _ = _collect(
            [
                b"npm warn see https://a.example/one\n"
                b"npm warn see https://b.example/two\n"
            ],
            fingerprinter=identity,
        )
        self.assertEqual(live[0].hostnames, ("a.example",))
        self.assertEqual(live[1].hostnames, ("b.example",))
        self.assertNotEqual(
            live[0].url_fingerprints, live[1].url_fingerprints
        )

    def test_make_stream_factory_wires_on_chunk(self):
        received: list[int] = []
        factory = make_stream_factory((), on_chunk=lambda: received.append(1))
        stream = factory("stdout")
        stream.feed_bytes(b"one\n")
        stream.feed_bytes(b"two\n")
        stream.finish()
        self.assertEqual(len(received), 2)

    def test_no_presentation_timer_owned_by_the_stream(self):
        source = inspect.getsource(NpmDiagnosticStream)
        self.assertNotIn("Thread(", source)
        self.assertNotIn("Timer(", source)
        self.assertNotIn("coalesc", source.lower())

    def test_structured_diagnostic_is_the_selection_type(self):
        # npm text must never be offered as a Constructor lifecycle type.
        self.assertNotIn(
            "HostPhaseEvent", inspect.getsource(classify_npm_diagnostic)
        )
        self.assertTrue(issubclass(HostStructuredDiagnostic, object))


class TestConsumablePerLineFacts(unittest.TestCase):
    """Per-line fact consumption, repeated hosts, and retention bounds."""

    def test_repeated_identical_lines_each_carry_the_hostname(self):
        # The same host on later lines must not lose its metadata just because
        # it first appeared on an earlier line.
        identity = SessionUrlIdentity()
        live, tail = _collect(
            [b"npm warn see https://registry.example.com/pkg\n" * 3],
            fingerprinter=identity,
        )
        self.assertEqual(len(live), 3)
        for chunk in live:
            self.assertEqual(chunk.hostnames, ("registry.example.com",))
            self.assertEqual(len(chunk.url_fingerprints), 1)
        self.assertNotIn("registry.example.com", tail)

    def test_multiple_urls_preserve_fingerprint_order_and_repeats(self):
        identity = SessionUrlIdentity()
        live, _ = _collect(
            [
                b"npm warn a https://a.example/one https://b.example/two "
                b"https://a.example/one\n"
            ],
            fingerprinter=identity,
        )
        chunk = live[0]
        self.assertEqual(chunk.hostnames, ("a.example", "b.example"))
        self.assertEqual(len(chunk.url_fingerprints), 3)
        # Order and repeated occurrences are preserved verbatim.
        self.assertEqual(
            chunk.url_fingerprints[0], chunk.url_fingerprints[2]
        )
        self.assertNotEqual(
            chunk.url_fingerprints[0], chunk.url_fingerprints[1]
        )

    def test_chunk_boundaries_do_not_change_line_metadata(self):
        identity = SessionUrlIdentity()
        data = (
            b"npm warn a https://a.example/one b https://b.example/two "
            b"c https://a.example/one\n"
        )
        whole, _ = _collect([data], fingerprinter=identity)
        bytewise, _ = _collect(
            [data[index : index + 1] for index in range(len(data))],
            fingerprinter=identity,
        )
        self.assertEqual(
            [(chunk.hostnames, chunk.url_fingerprints) for chunk in whole],
            [(chunk.hostnames, chunk.url_fingerprints) for chunk in bytewise],
        )

    def test_long_running_stream_retains_no_completed_line_metadata(self):
        identity = SessionUrlIdentity()
        stream = NpmDiagnosticStream("stdout", (), fingerprinter=identity)
        line = b"npm warn see https://registry.example.com/pkg/a\n"
        for _ in range(2000):
            produced = stream.feed_bytes(line)
            self.assertEqual(len(produced), 1)
            self.assertEqual(produced[0].hostnames, ("registry.example.com",))
            # The projector was drained for the completed line, so no metadata
            # survives into the next one.
            self.assertEqual(stream._projector.take_facts(), ((), ()))
            self.assertEqual(stream._line_hostnames, [])
            self.assertEqual(stream._line_fingerprints, [])
        self.assertEqual(stream.finish(), ())
        self.assertEqual(stream._projector.take_facts(), ((), ()))

    def test_oversized_url_heavy_line_discards_metadata_and_recovers(self):
        identity = SessionUrlIdentity()
        # Enough URL-bearing and plain text to exceed the 64 KiB *projected*
        # line limit, followed by a fresh line that must recover metadata.
        oversized = (
            b"npm warn "
            + b"https://a.example/p " * 100
            + b"pad " * 20000
            + b"\n"
        )
        stream = NpmDiagnosticStream("stdout", (), fingerprinter=identity)
        live: list[StreamChunk] = []
        observed_sanitizer: list[int] = []
        observed_line: list[int] = []
        payload = oversized + b"npm warn see https://b.example/ok\n"
        for index in range(0, len(payload), 4096):
            live.extend(stream.feed_bytes(payload[index : index + 4096]))
            observed_sanitizer.append(stream.pending_sanitizer_bytes)
            observed_line.append(stream.pending_line_bytes)
        live.extend(stream.finish())
        self.assertLessEqual(max(observed_sanitizer), 8 * 1024)
        self.assertLessEqual(max(observed_line), DIAGNOSTIC_LINE_LIMIT_BYTES)
        markers = [
            chunk for chunk in live if chunk.text == OVERSIZED_DIAGNOSTIC_MARKER
        ]
        self.assertEqual(len(markers), 1)
        # The fixed marker carries none of the discarded line's metadata.
        self.assertEqual(markers[0].hostnames, ())
        self.assertEqual(markers[0].url_fingerprints, ())
        self.assertNotIn(
            "a.example", [host for chunk in live for host in chunk.hostnames]
        )
        self.assertNotIn("a.example", stream.tail())
        recovered = [chunk for chunk in live if chunk.text.startswith("npm warn see")]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].hostnames, ("b.example",))
        self.assertEqual(len(recovered[0].url_fingerprints), 1)
        self.assertEqual(stream._projector.take_facts(), ((), ()))


class TestCollectionAbortFinalization(unittest.TestCase):
    """A forced EOF must fail closed; a clean EOF stays unchanged.

    The reader workers must finalize an ambiguous pending URL/secret prefix
    with :data:`INCOMPLETE_TOKEN_MARKER` whenever cancellation (interruption,
    sibling-reader failure, or caller-driven termination) forces them to EOF,
    and must preserve the historical clean-EOF flush otherwise.
    """

    _PREFIX = b"npm status %68%74"

    @staticmethod
    def _capturing_factory(created):
        base = make_stream_factory(())

        def factory(stream):
            instance = base(stream)
            created[stream] = instance
            return instance

        return factory

    @staticmethod
    def _blocked_reader(prefix, fed, release):
        def read(n: int) -> bytes:
            if not fed.is_set():
                fed.set()
                return prefix
            release.wait(5.0)
            return b""

        return read

    def test_clean_eof_control_flushes_the_prefix(self):
        buffer = io.BytesIO(self._PREFIX)
        capture = collect_streams(
            stdout_read=lambda n: buffer.read(n),
            stderr_read=lambda n: b"",
            stream_factory=make_stream_factory(()),
            tail_projector=project_tail,
        )
        self.assertIn("%68%74", capture.stdout_tail)
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, capture.stdout_tail)
        _assert_no_workers(self)

    def test_sibling_reader_failure_fails_closed(self):
        created: dict = {}
        fed = threading.Event()
        release = threading.Event()
        delivered: list[StreamChunk] = []

        def read_stderr(n: int) -> bytes:
            raise OSError("stderr reader died")

        def on_reader_failure(stream: str) -> None:
            release.set()

        with self.assertRaises(StreamReaderFailure):
            collect_streams(
                stdout_read=self._blocked_reader(self._PREFIX, fed, release),
                stderr_read=read_stderr,
                sink=delivered.append,
                stream_factory=self._capturing_factory(created),
                tail_projector=project_tail,
                on_reader_failure=on_reader_failure,
            )
        live = "".join(chunk.text for chunk in delivered)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, live)
        self.assertNotIn("%68%74", live)
        tail = created["stdout"].tail()
        self.assertIn(INCOMPLETE_TOKEN_MARKER, tail)
        self.assertNotIn("%68%74", tail)
        _assert_no_workers(self)

    def test_interruption_fails_closed(self):
        created: dict = {}
        fed = threading.Event()
        release = threading.Event()
        delivered: list[StreamChunk] = []

        def read_stderr(n: int) -> bytes:
            release.wait(5.0)
            return b""

        def on_interruption() -> None:
            release.set()

        def interrupt() -> None:
            self.assertTrue(fed.wait(5.0))
            time.sleep(0.1)
            signal.pthread_kill(threading.main_thread().ident, signal.SIGINT)

        timer = threading.Thread(target=interrupt, daemon=True)
        timer.start()
        with self.assertRaises(KeyboardInterrupt):
            collect_streams(
                stdout_read=self._blocked_reader(self._PREFIX, fed, release),
                stderr_read=read_stderr,
                sink=delivered.append,
                stream_factory=self._capturing_factory(created),
                tail_projector=project_tail,
                on_interruption=on_interruption,
            )
        timer.join(5.0)
        live = "".join(chunk.text for chunk in delivered)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, live)
        self.assertNotIn("%68%74", live)
        tail = created["stdout"].tail()
        self.assertIn(INCOMPLETE_TOKEN_MARKER, tail)
        self.assertNotIn("%68%74", tail)
        _assert_no_workers(self)

    def test_caller_abort_signal_reaches_both_readers(self):
        # A deadline supervisor owns this signal: it is set before the client
        # is terminated/closing pipes, and both readers that reach the forced
        # EOF must fail closed independently.
        created: dict = {}
        fed_out = threading.Event()
        fed_err = threading.Event()
        release = threading.Event()
        abort = threading.Event()
        delivered: list[StreamChunk] = []

        def read_stdout(n: int) -> bytes:
            if not fed_out.is_set():
                fed_out.set()
                return b"npm status %68%74"
            release.wait(5.0)
            return b""

        def read_stderr(n: int) -> bytes:
            if not fed_err.is_set():
                fed_err.set()
                return b"npm error %68%74"
            release.wait(5.0)
            return b""

        def cancel() -> None:
            self.assertTrue(fed_out.wait(5.0) and fed_err.wait(5.0))
            time.sleep(0.1)
            abort.set()
            release.set()

        helper = threading.Thread(target=cancel, daemon=True)
        helper.start()
        capture = collect_streams(
            stdout_read=read_stdout,
            stderr_read=read_stderr,
            sink=delivered.append,
            stream_factory=self._capturing_factory(created),
            tail_projector=project_tail,
            abort_event=abort,
        )
        helper.join(5.0)
        live = "".join(chunk.text for chunk in delivered)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, live)
        self.assertNotIn("%68%74", live)
        for tail in (
            capture.stdout_tail,
            capture.stderr_tail,
            created["stdout"].tail(),
            created["stderr"].tail(),
        ):
            self.assertIn(INCOMPLETE_TOKEN_MARKER, tail)
            self.assertNotIn("%68%74", tail)
        _assert_no_workers(self)


def _assert_no_workers(testcase: unittest.TestCase) -> None:
    """Assert the collector's reader and dispatcher workers have exited."""
    live = {thread.name for thread in threading.enumerate()}
    for name in (
        "npm-stdout-reader",
        "npm-stderr-reader",
        "npm-sink-dispatcher",
    ):
        testcase.assertNotIn(name, live)


if __name__ == "__main__":
    unittest.main()
