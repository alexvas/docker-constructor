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
from docker.versioning.model import NetworkUrlDisplay
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
    """Feed *chunks* through one stream and return (finalized lines, tail).

    Only finalized complete-line events are returned; committed prefixes are
    exercised separately by :func:`_collect_all`.
    """
    stream = NpmDiagnosticStream(
        "stdout", secrets, fingerprinter=fingerprinter, tail_bytes=tail_bytes
    )
    live: list[StreamChunk] = []
    for chunk in chunks:
        live.extend(stream.feed_bytes(chunk))
    live.extend(stream.finish())
    finalized = [chunk for chunk in live if chunk.finalized]
    return finalized, stream.tail()


def _collect_all(chunks, *, secrets=(), fingerprinter=None, tail_bytes=TAIL_BYTES):
    """Feed *chunks* through one stream and return (all chunks, tail)."""
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
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        payload = (
            b"npm notice "
            + b"word " * ((limit - 11) // 5)
            + b"DISCARDME" * 2000
        )
        for index in range(0, len(payload), 1024):
            live.extend(stream.feed_bytes(payload[index : index + 1024]))
            observed.append(stream.pending_line_bytes)
        live.extend(stream.finish())
        self.assertLessEqual(max(observed), limit)
        joined = "".join(chunk.text for chunk in live)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, joined)
        # The committed prefix is delivered; only the suffix is discarded.
        self.assertIn("word word", joined)
        self.assertNotIn("DISCARDME", joined)
        self.assertNotIn("DISCARDME", stream.tail())

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
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        payload = (
            b"npm notice "
            + b"frag " * ((limit - 11) // 5)
            + b"DISCARDED" * 2000
            + b"\nok\n"
        )
        _, tail = _collect([payload])
        self.assertNotIn("DISCARDED", tail)
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
            produced = [
                chunk
                for chunk in stream.feed_bytes(line)
                if chunk.finalized
            ]
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
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        # A plain committed prefix at the bound followed by an oversized
        # URL-bearing suffix that must be discarded, then a fresh line that
        # must recover metadata.
        prefix = b"npm warn " + b"pad " * ((limit - 9) // 4) + b"pad"[: (limit - 9) % 4]
        oversized = prefix + b" https://a.example/secret" * 100 + b"\n"
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
        self.assertLessEqual(max(observed_line), limit)
        finalized = [chunk for chunk in live if chunk.finalized]
        markers = [
            chunk for chunk in finalized if OVERSIZED_DIAGNOSTIC_MARKER in chunk.text
        ]
        self.assertEqual(len(markers), 1)
        # The fixed marker carries none of the discarded line's metadata.
        self.assertEqual(markers[0].hostnames, ())
        self.assertEqual(markers[0].url_fingerprints, ())
        self.assertNotIn(
            "a.example", [host for chunk in finalized for host in chunk.hostnames]
        )
        self.assertNotIn("a.example", stream.tail())
        recovered = [
            chunk
            for chunk in finalized
            if chunk.text.startswith("npm warn see")
        ]
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
    _HOST_PATH_PREFIX = b"hello https://example.com/path"

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

    @staticmethod
    def _host_path_factory(created):
        base = make_stream_factory(
            (), network_url_display=NetworkUrlDisplay.HOST_PATH
        )

        def factory(stream):
            instance = base(stream)
            created[stream] = instance
            return instance

        return factory

    def _assert_host_path_fail_closed(self, delivered, created):
        expected = f"hello {INCOMPLETE_TOKEN_MARKER}"
        finalized = [chunk for chunk in delivered if chunk.finalized]
        self.assertEqual(1, len(finalized), finalized)
        self.assertEqual(expected, finalized[0].local_text)
        self.assertEqual(expected, created["stdout"].tail())
        # The structured/SDK-facing text and the retained tail are URL-free.
        self.assertNotIn("example.com", finalized[0].text)
        self.assertNotIn("example.com", finalized[0].local_text)
        self.assertNotIn("example.com", created["stdout"].tail())

    def test_sibling_reader_failure_host_path_fails_closed(self):
        created: dict = {}
        fed = threading.Event()
        release = threading.Event()
        delivered: list[StreamChunk] = []

        def read_stderr(n: int) -> bytes:
            raise OSError("stderr reader died")

        with self.assertRaises(StreamReaderFailure):
            collect_streams(
                stdout_read=self._blocked_reader(
                    self._HOST_PATH_PREFIX, fed, release
                ),
                stderr_read=read_stderr,
                sink=delivered.append,
                stream_factory=self._host_path_factory(created),
                tail_projector=project_tail,
                network_url_display=NetworkUrlDisplay.HOST_PATH,
                on_reader_failure=lambda stream: release.set(),
            )
        self._assert_host_path_fail_closed(delivered, created)
        _assert_no_workers(self)

    def test_interruption_host_path_fails_closed(self):
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
                stdout_read=self._blocked_reader(
                    self._HOST_PATH_PREFIX, fed, release
                ),
                stderr_read=read_stderr,
                sink=delivered.append,
                stream_factory=self._host_path_factory(created),
                tail_projector=project_tail,
                network_url_display=NetworkUrlDisplay.HOST_PATH,
                on_interruption=on_interruption,
            )
        timer.join(5.0)
        self._assert_host_path_fail_closed(delivered, created)
        _assert_no_workers(self)


class TestCommittedPrefixDelivery(unittest.TestCase):
    """Task 2.9/2.10 -- prompt committed-prefix release, structured path.

    The structured stream must expose terminal-safe committed prefixes as soon
    as the decoder, neutralizer, and projector commit them -- before any
    newline or ``finish()`` -- while withholding incomplete control
    sequences, split UTF-8, partial secrets, and unresolved URL candidates.
    """

    def test_safe_prefix_is_observable_before_newline(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes(b"npm warn partial")
        self.assertTrue(chunks)
        self.assertTrue(all(not chunk.finalized for chunk in chunks))
        # The trailing scheme-like word is conservatively withheld as a
        # possible URL prefix; the already-committed text is released now.
        self.assertEqual("npm warn ", "".join(chunk.text for chunk in chunks))
        self.assertEqual(stream.tail(), "npm warn ")
        self.assertEqual(stream.pending_line_bytes, len(b"npm warn partial"))
        final = stream.finish()
        self.assertEqual(
            "partial",
            "".join(chunk.text for chunk in final if not chunk.finalized),
        )
        self.assertEqual(
            [chunk.text for chunk in final if chunk.finalized],
            ["npm warn partial"],
        )
        # The withheld suffix is retained exactly once when it is committed.
        self.assertEqual(stream.tail(), "npm warn partial")

    def test_split_utf8_prefix_releases_only_committed_bytes(self):
        stream = NpmDiagnosticStream("stdout")
        first = "".join(
            chunk.text for chunk in stream.feed_bytes(b"npm \xe2")
        )
        self.assertEqual(first, "npm ")
        second = "".join(
            chunk.text for chunk in stream.feed_bytes(b"\x82\xac")
        )
        self.assertEqual(second, "\u20ac")
        self.assertEqual(stream.tail(), "npm \u20ac")

    def test_fragmented_terminal_control_releases_only_committed_text(self):
        stream = NpmDiagnosticStream("stdout")
        first = "".join(
            chunk.text for chunk in stream.feed_bytes(b"npm \x1b[31")
        )
        self.assertEqual(first, "npm ")
        self.assertNotIn("\x1b", first)
        second = "".join(
            chunk.text
            for chunk in stream.feed_bytes(b"mred\x1b[0m")
        )
        self.assertEqual(second, r"\x1b[31mred\x1b[0m")
        self.assertNotIn("\x1b", second)

    def test_secret_prefix_is_withheld_until_committed(self):
        stream = NpmDiagnosticStream("stdout", ("s3cr3t",))
        first = "".join(
            chunk.text for chunk in stream.feed_bytes(b"npm token s3c")
        )
        self.assertEqual(first, "npm token ")
        self.assertNotIn("s3c", first)
        second = "".join(
            chunk.text
            for chunk in stream.feed_bytes(b"r3t done\n")
        )
        self.assertIn(_REDACTED, second)
        self.assertNotIn("s3cr3t", second)
        self.assertNotIn("s3cr3t", stream.tail())

    def test_url_candidate_is_withheld_until_committed(self):
        identity = SessionUrlIdentity()
        stream = NpmDiagnosticStream("stdout", (), fingerprinter=identity)
        first = "".join(
            chunk.text
            for chunk in stream.feed_bytes(
                b"npm see https://registry.example.com"
            )
        )
        self.assertEqual(first, "npm see ")
        self.assertNotIn("registry.example.com", first)
        second = "".join(
            chunk.text for chunk in stream.feed_bytes(b"/pkg done\n")
        )
        self.assertNotIn("registry.example.com", second)
        self.assertIn(_REDACTED, second)
        self.assertNotIn("registry.example.com", stream.tail())

    def test_finalization_does_not_retain_or_deliver_prefix_twice(self):
        stream = NpmDiagnosticStream("stdout")
        all_chunks = list(stream.feed_bytes(b"npm warn one\n"))
        all_chunks.extend(stream.finish())
        self.assertEqual(stream.tail(), "npm warn one\n")
        finalized = [chunk for chunk in all_chunks if chunk.finalized]
        self.assertEqual([chunk.text for chunk in finalized], ["npm warn one"])
        prefixes = "".join(
            chunk.text for chunk in all_chunks if not chunk.finalized
        )
        self.assertEqual(prefixes, "npm warn one")


def _feed_prefix_chunks(
    payload: bytes,
    mode: NetworkUrlDisplay,
    *,
    secrets: tuple[str, ...] = (),
) -> tuple[NpmDiagnosticStream, list[StreamChunk], str]:
    """Feed *payload* (no newline) and return (stream, chunks, local text)."""
    stream = NpmDiagnosticStream(
        "stdout", secrets, network_url_display=mode
    )
    chunks = list(stream.feed_bytes(payload))
    local = "".join(
        chunk.local_text
        for chunk in chunks
        if chunk.local_text is not None
    )
    return stream, chunks, local


class TestCommittedPrefixDisplayModes(unittest.TestCase):
    """Committed prefixes use the mode-selected display representation.

    A partial ``host-path`` or ``exact`` diagnostic must render in its
    selected representation before its newline, not redacted, while the
    structured/SDK-facing ``text`` stays URL-free in every mode.
    """

    _URL = (
        "https://user:secret@registry.example.com:8443"
        "/pkg/-/pkg-1.0.0.tgz?q=1#frag"
    )
    _LINE = f"npm error network GET {_URL} failed"
    _HOST_PATH = "registry.example.com/pkg/-/pkg-1.0.0.tgz"

    def test_redacted_prefix_is_url_free_and_matches_the_tail(self):
        stream, chunks, local = _feed_prefix_chunks(
            self._LINE.encode(), NetworkUrlDisplay.REDACTED, secrets=("secret",)
        )
        self.assertTrue(chunks)
        self.assertTrue(all(not chunk.finalized for chunk in chunks))
        self.assertIn("npm error network GET", local)
        self.assertIn(_REDACTED, local)
        self.assertNotIn("registry.example.com", local)
        self.assertNotIn("user:secret", local)
        self.assertNotIn(":8443", local)
        self.assertNotIn("?q=1", local)
        self.assertNotIn("#frag", local)
        # ``redacted`` selects the URL-free projection as its local text.
        self.assertTrue(all(chunk.local_text == chunk.text for chunk in chunks))
        self.assertEqual(stream.tail(), local)

    def test_host_path_prefix_uses_host_and_path_and_matches_the_tail(self):
        stream, chunks, local = _feed_prefix_chunks(
            self._LINE.encode(),
            NetworkUrlDisplay.HOST_PATH,
            secrets=("secret",),
        )
        self.assertIn(self._HOST_PATH, local)
        self.assertNotIn("https://", local)
        self.assertNotIn("user:secret", local)
        self.assertNotIn(":8443", local)
        self.assertNotIn("?q=1", local)
        self.assertNotIn("#frag", local)
        # The structured/SDK-facing text never carries the source host/path.
        safe = "".join(chunk.text for chunk in chunks)
        self.assertNotIn("registry.example.com", safe)
        self.assertNotIn("https://", safe)
        # The selected prefix matches the one retained host-path tail.
        self.assertEqual(stream.tail(), local)

    def test_exact_prefix_preserves_source_and_matches_the_tail(self):
        stream, _chunks, local = _feed_prefix_chunks(
            self._LINE.encode(), NetworkUrlDisplay.EXACT, secrets=("secret",)
        )
        self.assertIn(self._URL, local)
        self.assertIn("failed", local)
        self.assertEqual(stream.tail(), local)

    def test_exact_prefix_neutralizes_terminal_controls(self):
        _stream, _chunks, local = _feed_prefix_chunks(
            b"npm warn \x1b[31mred\x1b[0m", NetworkUrlDisplay.EXACT
        )
        self.assertNotIn("\x1b", local)
        self.assertIn(r"\x1b[31m", local)

    def test_selected_prefix_chunks_keep_structured_text_empty(self):
        for mode in (NetworkUrlDisplay.HOST_PATH, NetworkUrlDisplay.EXACT):
            with self.subTest(mode=mode):
                _stream, chunks, _local = _feed_prefix_chunks(
                    self._LINE.encode(), mode, secrets=("secret",)
                )
                presentation_only = [
                    chunk
                    for chunk in chunks
                    if chunk.local_text is not None
                ]
                self.assertTrue(presentation_only)
                self.assertTrue(
                    all(chunk.text == "" for chunk in presentation_only)
                )
                # Any prefix that advances the safe projection carries no
                # selected local fragment and no source host/URL.
                for chunk in chunks:
                    if chunk.local_text is None:
                        self.assertNotIn("registry.example.com", chunk.text)
                        self.assertNotIn("https://", chunk.text)

    def test_fragmented_url_prefix_uses_the_selected_representation(self):
        expectations = {
            NetworkUrlDisplay.REDACTED: (_REDACTED, "registry.example.com"),
            NetworkUrlDisplay.HOST_PATH: (self._HOST_PATH, "https://"),
            NetworkUrlDisplay.EXACT: (self._URL, None),
        }
        for mode, (required, forbidden) in expectations.items():
            with self.subTest(mode=mode):
                stream = NpmDiagnosticStream(
                    "stdout", ("secret",), network_url_display=mode
                )
                local_parts: list[str] = []
                safe_parts: list[str] = []
                for byte in self._LINE.encode():
                    for chunk in stream.feed_bytes(bytes([byte])):
                        if chunk.local_text is not None:
                            local_parts.append(chunk.local_text)
                        safe_parts.append(chunk.text)
                local = "".join(local_parts)
                self.assertIn(required, local)
                self.assertEqual(stream.tail(), local)
                if forbidden is not None:
                    self.assertNotIn(forbidden, local)
                # The URL-free structured/SDK text never carries the source.
                self.assertNotIn("https://", "".join(safe_parts))

    def test_fragmented_secret_prefix_never_leaks_before_the_newline(self):
        stream = NpmDiagnosticStream(
            "stdout",
            ("s3cr3t-token",),
            network_url_display=NetworkUrlDisplay.REDACTED,
        )
        local_parts: list[str] = []
        for byte in b"npm warn token s3cr3t-token done":
            for chunk in stream.feed_bytes(bytes([byte])):
                if chunk.local_text is not None:
                    local_parts.append(chunk.local_text)
        local = "".join(local_parts)
        self.assertNotIn("s3cr3t-token", local)
        self.assertIn(_REDACTED, local)
        self.assertEqual(stream.tail(), local)

    def test_split_utf8_prefix_uses_the_selected_representation(self):
        for mode in (
            NetworkUrlDisplay.REDACTED,
            NetworkUrlDisplay.HOST_PATH,
            NetworkUrlDisplay.EXACT,
        ):
            with self.subTest(mode=mode):
                stream = NpmDiagnosticStream(
                    "stdout", (), network_url_display=mode
                )
                first = list(stream.feed_bytes(b"npm \xe2"))
                self.assertEqual(
                    "npm ",
                    "".join(
                        chunk.local_text
                        for chunk in first
                        if chunk.local_text is not None
                    ),
                )
                second = list(stream.feed_bytes(b"\x82\xac"))
                local = "".join(
                    chunk.local_text
                    for chunk in first + second
                    if chunk.local_text is not None
                )
                self.assertIn("\u20ac", local)
                self.assertEqual(stream.tail(), local)


class TestAbortFailClosedHostPathFinalization(unittest.TestCase):
    """An abort must never resolve a token the live sanitizer withheld.

    A ``host-path`` line finalized at reader failure or cancellation uses the
    same fail-closed EOF policy as the retained tail, so the finalized live
    text and the retained tail can never diverge and an incomplete trailing
    URL token stays withheld.  A clean EOF or a record newline keeps the
    normal complete-URL resolution.
    """

    _SOURCE = b"hello https://example.com/path"
    _FAIL_CLOSED = f"hello {INCOMPLETE_TOKEN_MARKER}"
    _RESOLVED = "hello example.com/path"

    def _finalize(self, payload: bytes, *, abort: bool):
        stream = NpmDiagnosticStream(
            "stdout", (), network_url_display=NetworkUrlDisplay.HOST_PATH
        )
        chunks = list(stream.feed_bytes(payload))
        chunks.extend(stream.finish(abort=abort))
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized), finalized)
        return stream, finalized[0]

    def test_abort_keeps_live_and_retained_fail_closed(self):
        stream, chunk = self._finalize(self._SOURCE, abort=True)
        self.assertEqual(self._FAIL_CLOSED, chunk.local_text)
        self.assertEqual(self._FAIL_CLOSED, stream.tail())
        self.assertNotIn("example.com", chunk.local_text)
        self.assertNotIn("example.com", stream.tail())
        # The structured/SDK-facing text fails closed as well.
        self.assertEqual(self._FAIL_CLOSED, chunk.text)

    def test_abort_covers_ambiguous_scheme_prefixes(self):
        for source in ("hello htt", "hello https", "hello https:"):
            with self.subTest(source=source):
                stream, chunk = self._finalize(source.encode(), abort=True)
                self.assertEqual(self._FAIL_CLOSED, chunk.local_text)
                self.assertEqual(chunk.local_text, stream.tail())
                self.assertNotIn("htt", chunk.local_text)

    def test_clean_eof_still_resolves_a_complete_url(self):
        stream, chunk = self._finalize(self._SOURCE, abort=False)
        self.assertEqual(self._RESOLVED, chunk.local_text)
        self.assertEqual(self._RESOLVED, stream.tail())

    def test_newline_finalization_still_resolves_a_complete_url(self):
        stream, chunk = self._finalize(self._SOURCE + b"\n", abort=False)
        self.assertEqual(self._RESOLVED, chunk.local_text)
        # The retained tail preserves the record boundary, so live and tail
        # agree on the selected text.
        self.assertEqual(self._RESOLVED + "\n", stream.tail())


class TestHostPathRecordBoundaryFinalization(unittest.TestCase):
    """A record newline is not EOF: live and retained text must agree.

    The ``host-path`` local text is accumulated incrementally from the
    selected projector rather than reprojected from a retained source line,
    and the record boundary is threaded through so the two agree.  A newline
    terminates a record (a trailing secret or scheme prefix before it is
    ordinary text), EOF keeps the projector's normal finish policy, and only
    the record newline is dropped from the finalized diagnostic text.
    """

    def _finalize(
        self, payload: bytes, *, secrets: tuple[str, ...] = (), abort: bool = False
    ):
        stream = NpmDiagnosticStream(
            "stdout", secrets, network_url_display=NetworkUrlDisplay.HOST_PATH
        )
        chunks = list(stream.feed_bytes(payload))
        chunks.extend(stream.finish(abort=abort))
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized), finalized)
        return stream, finalized[0]

    def _assert_agrees_with_retained(self, payload, expected, **kwargs):
        stream, chunk = self._finalize(payload, **kwargs)
        self.assertEqual(expected, chunk.local_text)
        tail = stream.tail()
        # The retained line keeps its terminator; the finalized live text does
        # not.  Compare everything else exactly.
        if payload.endswith(b"\n"):
            self.assertEqual(expected + "\n", tail)
        else:
            self.assertEqual(expected, tail)
        return chunk

    def test_newline_terminated_partial_secret_is_ordinary_text(self):
        chunk = self._assert_agrees_with_retained(
            b"hello sec\n", "hello sec", secrets=("secret",)
        )
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, chunk.local_text)

    def test_newline_terminated_scheme_prefix_is_ordinary_text(self):
        self._assert_agrees_with_retained(b"hello git:\n", "hello git:")

    def test_newline_terminated_bare_url_prefix_matches_retained(self):
        stream, chunk = self._finalize(b"hello https://\n")
        self.assertEqual(chunk.local_text, stream.tail()[: -len("\n")])
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, chunk.local_text)

    def test_clean_eof_partial_secret_still_fails_closed(self):
        chunk = self._assert_agrees_with_retained(
            b"hello sec",
            f"hello {INCOMPLETE_TOKEN_MARKER}",
            secrets=("secret",),
        )
        self.assertIn(INCOMPLETE_TOKEN_MARKER, chunk.local_text)

    def test_aborted_eof_partial_secret_still_fails_closed(self):
        self._assert_agrees_with_retained(
            b"hello sec",
            f"hello {INCOMPLETE_TOKEN_MARKER}",
            secrets=("secret",),
            abort=True,
        )

    def test_aborted_eof_keeps_a_complete_url_fail_closed(self):
        self._assert_agrees_with_retained(
            b"hello https://example.com/path",
            f"hello {INCOMPLETE_TOKEN_MARKER}",
            abort=True,
        )

    def test_clean_eof_bare_scheme_word_is_ordinary_text(self):
        self._assert_agrees_with_retained(b"hello git", "hello git")

    def test_clean_eof_resolves_a_complete_url(self):
        self._assert_agrees_with_retained(
            b"hello https://example.com/path", "hello example.com/path"
        )

    def test_newline_terminated_complete_url_resolves_without_terminator(self):
        self._assert_agrees_with_retained(
            b"hello https://example.com/path\n", "hello example.com/path"
        )


class TestCompleteLineConsumers(unittest.TestCase):
    """Task 2.10 -- complete-line consumers see only finalized lines.

    npm fetch parsing, classification, identity, and grouping are bound to the
    ``finalized`` record boundary; a committed prefix never becomes a partial
    diagnostic event, and finalization never rewrites the retained tail.
    """

    _FIXTURE = (
        b"npm http fetch GET 200 "
        b"https://registry.npmjs.org/npm-http-research-fixture/-/"
        b"npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)\n"
    )

    def test_partial_prefix_produces_no_complete_line(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes(
            b"npm http fetch GET 200 https://registry.npmjs.org/npm-http"
        )
        self.assertTrue(chunks)
        self.assertTrue(all(not chunk.finalized for chunk in chunks))
        self.assertEqual(
            [chunk for chunk in chunks if chunk.finalized], []
        )

    def test_prefix_then_newline_yields_one_finalized_line(self):
        stream = NpmDiagnosticStream("stdout")
        produced = list(stream.feed_bytes(b"npm warn part"))
        self.assertTrue(produced)
        self.assertTrue(all(not chunk.finalized for chunk in produced))
        produced.extend(stream.feed_bytes(b"ial\n"))
        finalized = [chunk for chunk in produced if chunk.finalized]
        self.assertEqual([chunk.text for chunk in finalized], ["npm warn partial"])
        self.assertEqual(stream.tail(), "npm warn partial\n")

    def test_research_fixture_is_one_finalized_event(self):
        identity = SessionUrlIdentity()
        all_chunks, _tail = _collect_all(
            [self._FIXTURE], fingerprinter=identity
        )
        finalized = [chunk for chunk in all_chunks if chunk.finalized]
        self.assertEqual(len(finalized), 1)
        text = finalized[0].text
        self.assertIn("npm http fetch GET 200", text)
        self.assertNotIn("registry.npmjs.org", text)
        self.assertEqual(finalized[0].hostnames, ("registry.npmjs.org",))
        self.assertEqual(len(finalized[0].url_fingerprints), 1)

    def test_three_lines_yield_three_finalized_events(self):
        identity = SessionUrlIdentity()
        payload = (
            b"npm warn see https://a.example/one\n"
            b"npm warn see https://b.example/two\n"
            b"npm warn see https://a.example/three\n"
        )
        live, tail = _collect([payload], fingerprinter=identity)
        self.assertEqual(len(live), 3)
        self.assertEqual(
            [chunk.hostnames for chunk in live],
            [("a.example",), ("b.example",), ("a.example",)],
        )
        self.assertEqual(tail.count("npm warn see"), 3)


class TestFetchCanonicalizationSecretSafety(unittest.TestCase):
    """A canonical fetch rendering must never restore a project secret.

    ``canonical_fetch_text`` sanitizes only the URL-derived host/path.  Its
    method, exact status, and optional attempt and cache clauses are copied
    from the source line, so a configured secret in any of them was redacted
    from the selected diagnostic and would be restored by the canonical text.
    The collector must reject that rendering, keep the sanitized ordinary
    diagnostic, and refuse to group.
    """

    _URL = "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz"

    def _line(
        self,
        *,
        method: str = "GET",
        status: int = 200,
        attempt: int | None = None,
        cache: str | None = "miss",
    ) -> str:
        body = f"npm http fetch {method} {status} {self._URL} 15ms"
        if attempt is not None:
            body += f" attempt #{attempt}"
        if cache is not None:
            body += f" (cache {cache})"
        return body

    def _finalize(
        self,
        line: str,
        *,
        secrets: tuple[str, ...],
        display: NetworkUrlDisplay,
    ) -> tuple[StreamChunk, str]:
        stream = NpmDiagnosticStream(
            "stdout", secrets, network_url_display=display
        )
        chunks = list(stream.feed_bytes((line + "\n").encode("utf-8")))
        chunks.extend(stream.finish())
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized), finalized)
        return finalized[0], stream.tail()

    def _assert_falls_back(
        self, line: str, secret: str, display: NetworkUrlDisplay
    ) -> StreamChunk:
        chunk, tail = self._finalize(line, secrets=(secret,), display=display)
        self.assertIsNone(chunk.fetch_key)
        self.assertIsNone(chunk.fetch_text)
        for text in (chunk.text, chunk.local_text or "", tail):
            self.assertNotIn(secret, text)
        return chunk

    def test_cache_secret_falls_back_in_every_mode(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                chunk = self._assert_falls_back(
                    self._line(cache="miss"), "miss", display
                )
                self.assertIn(_REDACTED, chunk.local_text or "")

    def test_method_and_status_secrets_fall_back(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display, field="method"):
                self._assert_falls_back(self._line(method="GET"), "GET", display)
            with self.subTest(display=display, field="status"):
                self._assert_falls_back(self._line(status=200), "20", display)

    def test_attempt_and_substring_secrets_fall_back(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display, field="attempt"):
                self._assert_falls_back(self._line(attempt=3), "#3", display)
            with self.subTest(display=display, field="substring"):
                self._assert_falls_back(
                    self._line(cache="topsecret"), "sec", display
                )

    def test_exact_cache_token_secret_falls_back_in_every_mode(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                self._assert_falls_back(
                    self._line(cache="topsecret"), "topsecret", display
                )

    def test_secret_spanning_two_source_fields_falls_back(self):
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                self._assert_falls_back(
                    self._line(method="GET", status=200), "GET 200", display
                )

    def test_unrelated_secret_still_aggregates(self):
        chunk, _tail = self._finalize(
            self._line(cache="miss"),
            secrets=("unrelated",),
            display=NetworkUrlDisplay.REDACTED,
        )
        self.assertIsNotNone(chunk.fetch_key)
        self.assertEqual(
            "npm http fetch GET 200 <redacted> (cache miss)", chunk.fetch_text
        )


def _assert_no_workers(testcase: unittest.TestCase) -> None:
    """Assert the collector's reader and dispatcher workers have exited."""
    live = {thread.name for thread in threading.enumerate()}
    for name in (
        "npm-stdout-reader",
        "npm-stderr-reader",
        "npm-sink-dispatcher",
    ):
        testcase.assertNotIn(name, live)


class TestOverflowBoundaryTagging(unittest.TestCase):
    """An oversized line is tagged so it is never classified or grouped."""

    def test_overflow_finalized_line_is_tagged(self):
        stream = NpmDiagnosticStream("stdout")
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        prefix = b"pad " * (limit // 4)
        chunks = list(stream.feed_bytes(prefix + b"DISCARDED" * 100))
        # The committed prefix and the one fixed marker are already
        # observable; the record boundary finalizes the truncated line.
        self.assertTrue(any(chunk.overflowed for chunk in chunks))
        self.assertEqual(stream.tail().count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        chunks.extend(stream.feed_bytes(b"\n"))
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized))
        self.assertTrue(finalized[0].overflowed)
        self.assertIn(OVERSIZED_DIAGNOSTIC_MARKER, finalized[0].text)
        self.assertNotIn("DISCARDED", finalized[0].text)
        # Recovery: the following complete line is an ordinary diagnostic.
        recovered = [
            chunk
            for chunk in stream.feed_bytes(b"npm warn ok\n")
            if chunk.finalized
        ]
        self.assertEqual(1, len(recovered))
        self.assertFalse(recovered[0].overflowed)

    def test_source_text_containing_the_marker_is_not_an_overflow(self):
        stream = NpmDiagnosticStream("stdout")
        payload = f"npm warn {OVERSIZED_DIAGNOSTIC_MARKER}\n".encode()
        finalized = [
            chunk for chunk in stream.feed_bytes(payload) if chunk.finalized
        ]
        self.assertEqual(1, len(finalized))
        self.assertFalse(finalized[0].overflowed)

    def test_overflow_suffix_and_metadata_never_reach_the_tail(self):
        identity = SessionUrlIdentity()
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        prefix = b"pad " * (limit // 4)
        payload = prefix + b" https://a.example/secret" * 100 + b"\n"
        live, tail = _collect_all([payload], fingerprinter=identity)
        finalized = [chunk for chunk in live if chunk.finalized]
        truncated = [chunk for chunk in finalized if chunk.overflowed]
        self.assertEqual(1, len(truncated))
        self.assertEqual(truncated[0].hostnames, ())
        self.assertEqual(truncated[0].url_fingerprints, ())
        self.assertNotIn("a.example", tail)
        self.assertEqual(tail.count(OVERSIZED_DIAGNOSTIC_MARKER), 1)


class TestOverflowFinalization(unittest.TestCase):
    """Stream termination after overflow never emits a second marker."""

    def test_eof_after_overflow_adds_no_second_marker(self):
        stream = NpmDiagnosticStream("stdout")
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        prefix = b"pad " * (limit // 4)
        stream.feed_bytes(prefix + b"DISCARDED" * 100)
        finalized = [
            chunk for chunk in stream.finish() if chunk.finalized
        ]
        self.assertEqual(1, len(finalized))
        self.assertTrue(finalized[0].overflowed)
        self.assertEqual(stream.tail().count(OVERSIZED_DIAGNOSTIC_MARKER), 1)
        self.assertNotIn("DISCARDED", stream.tail())


class TestIncrementalFetchRecognition(unittest.TestCase):
    """Recognition is incremental and retains no complete source line/URL.

    ``redacted`` and ``host-path`` never keep a terminal-safe source line: the
    collector feeds terminal-safe fragments to
    :class:`~docker.versioning.npm_fetch.NpmFetchRecognizer`, which retains
    only bounded parser fields, and ``host-path`` grouping reuses the
    projector's already-sanitized host/path fact.
    """

    _URL = "https://registry.npmjs.org/pkg/-/pkg-1.0.0.tgz"
    _LINE = f"npm http fetch GET 200 {_URL} 15ms (cache miss)"
    _RAW = _LINE.encode() + b"\n"

    def _stream(self, *, secrets=(), display=NetworkUrlDisplay.REDACTED):
        return NpmDiagnosticStream(
            "stdout", secrets, network_url_display=display
        )

    def _finalize(
        self,
        payload,
        *,
        secrets=(),
        display=NetworkUrlDisplay.REDACTED,
        chunk_size=None,
    ):
        stream = self._stream(secrets=secrets, display=display)
        chunks = []
        if chunk_size is None:
            chunks.extend(stream.feed_bytes(payload))
        else:
            for start in range(0, len(payload), chunk_size):
                chunks.extend(stream.feed_bytes(payload[start : start + chunk_size]))
        chunks.extend(stream.finish())
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized), finalized)
        return stream, chunks, finalized[0]

    def test_no_complete_source_buffer_is_retained(self):
        forbidden = {
            "_source_line_chars",
            "_source_line",
            "_raw_line",
            "_source_buffer",
            "_source_lines",
        }
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                stream = self._stream(display=display)
                live = stream.feed_bytes(self._LINE.encode())
                self.assertTrue(live)
                self.assertEqual(set(), set(vars(stream)) & forbidden)
                self.assertFalse(
                    any("source" in name for name in vars(stream)),
                    sorted(vars(stream)),
                )
                # Even mid-line, no attribute holds the complete raw URL.
                for value in vars(stream).values():
                    self.assertNotIn(self._URL, repr(value))
                stream.finish()

    def test_fragmented_fetch_line_groups_in_redacted(self):
        stream, _chunks, chunk = self._finalize(self._RAW, chunk_size=1)
        self.assertIsNotNone(chunk.fetch_key)
        self.assertEqual(
            NetworkUrlDisplay.REDACTED, chunk.fetch_key.display
        )
        self.assertEqual(
            "npm http fetch GET 200 <redacted> (cache miss)",
            chunk.fetch_text,
        )
        self.assertNotIn(self._URL, stream.tail())
        self.assertNotIn("registry.npmjs.org", stream.tail())

    def test_fragmented_fetch_line_groups_in_host_path(self):
        stream, _chunks, chunk = self._finalize(
            self._RAW, display=NetworkUrlDisplay.HOST_PATH, chunk_size=1
        )
        self.assertIsNotNone(chunk.fetch_key)
        self.assertEqual(
            NetworkUrlDisplay.HOST_PATH, chunk.fetch_key.display
        )
        self.assertEqual(
            "npm http fetch GET 200 "
            "registry.npmjs.org/pkg/-/pkg-1.0.0.tgz (cache miss)",
            chunk.fetch_text,
        )
        self.assertEqual(
            "registry.npmjs.org/pkg/-/pkg-1.0.0.tgz",
            chunk.fetch_key.host_path,
        )
        self.assertNotIn("https://", stream.tail())

    @staticmethod
    @staticmethod
    def _state_text(value) -> str:
        """Concatenate every string reachable from a state value.

        Container items are concatenated without a separator so that a raw
        authority buffer stored as a ``list[str]`` of characters is
        reconstructed rather than hidden.
        """
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple, set, frozenset)):
            return "".join(
                TestIncrementalFetchRecognition._state_text(item)
                for item in value
            )
        if isinstance(value, dict):
            return "".join(
                TestIncrementalFetchRecognition._state_text(key)
                + TestIncrementalFetchRecognition._state_text(item)
                for key, item in value.items()
            )
        if hasattr(value, "__dict__"):
            return "".join(
                TestIncrementalFetchRecognition._state_text(item)
                for item in vars(value).values()
            )
        return ""

    @classmethod
    def _retained_state_strings(cls, recognizer) -> str:
        return "\x00".join(
            cls._state_text(value) for value in vars(recognizer).values()
        )

    def test_parser_state_never_retains_credentials_query_or_fragment(self):
        from docker.versioning.npm_fetch import NpmFetchRecognizer

        url = (
            "https://credname:credsecret@example.com/secretpath"
            "?token=credq#credf"
        )
        line = f"npm http fetch GET 200 {url} 15ms"
        always_forbidden = (
            "credsecret",
            "credname:credsecret",
            "credname:credsecret@example.com",
            url,
            "secretpath",
            "token=credq",
            "#credf",
        )
        # Inspect the retained state after *every* fragment, not only after
        # the complete URL has cleared; a raw authority or user information
        # buffer would be caught at an intermediate boundary.
        for chunk_size in (None, 1, 2, 3, 7):
            with self.subTest(chunk_size=chunk_size):
                recognizer = NpmFetchRecognizer()
                if chunk_size is None:
                    recognizer.feed(line)
                else:
                    for start in range(0, len(line), chunk_size):
                        recognizer.feed(line[start : start + chunk_size])
                        retained = self._retained_state_strings(recognizer)
                        for fragment in always_forbidden:
                            self.assertNotIn(
                                fragment, retained, (chunk_size, start)
                            )
                        if "credname:credsecret@" in line[: start + chunk_size]:
                            self.assertNotIn("credname", retained)
                record = recognizer.finish()
                self.assertIsNotNone(record)
                retained = self._retained_state_strings(recognizer)
                for fragment in always_forbidden + ("credname",):
                    self.assertNotIn(fragment, retained)
                # The record carries only the parser-minimal fields.
                self.assertEqual("GET", record.method)
                self.assertEqual(200, record.status)
                self.assertIsNone(record.host_path)
                self.assertFalse(hasattr(record, "url"))

    def test_split_secret_in_a_source_field_disables_grouping(self):
        payload = (
            b"npm http fetch GET 200 "
            b"https://example.com/x 15ms (cache topsecret)\n"
        )
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                stream, _chunks, chunk = self._finalize(
                    payload, secrets=("topsecret",), display=display, chunk_size=1
                )
                self.assertIsNone(chunk.fetch_key)
                self.assertIsNone(chunk.fetch_text)
                self.assertNotIn("topsecret", chunk.text)
                self.assertNotIn("topsecret", chunk.local_text or "")
                self.assertNotIn("topsecret", stream.tail())

    def test_secret_spanning_parser_fields_disables_grouping(self):
        payload = self._RAW
        for display in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(display=display):
                stream, _chunks, chunk = self._finalize(
                    payload,
                    secrets=("GET 200",),
                    display=display,
                    chunk_size=2,
                )
                self.assertIsNone(chunk.fetch_key)
                self.assertIsNone(chunk.fetch_text)
                self.assertNotIn("GET 200", chunk.local_text or "")
                self.assertNotIn("GET 200", stream.tail())

    def test_malformed_and_incomplete_urls_remain_ordinary_diagnostics(self):
        malformed = (
            "npm http fetch GET 200 https://% 15ms",
            "npm http fetch GET 200 https:///x 15ms",
            "npm http fetch GET 200 http://:80/x 15ms",
            "npm http fetch GET 200 https://example.com:/x 15ms",
            "npm http fetch GET 200 https://example.com:99999/x 15ms",
            "npm http fetch GET 200 https:// 15ms",
            "npm http fetch GET 200 https://example.com 15",
            "npm http fetch GET 200 https://example.com 15ms attempt #",
            "npm http fetch GET 200 https://example.com 15ms (cache )",
        )
        for line in malformed:
            with self.subTest(line=line):
                _stream, _chunks, chunk = self._finalize(line.encode() + b"\n")
                self.assertIsNone(chunk.fetch_key)
                self.assertIsNone(chunk.fetch_text)

    def test_incomplete_url_at_eof_is_an_ordinary_diagnostic(self):
        stream, _chunks, chunk = self._finalize(b"npm http fetch GET 200 https://x")
        self.assertIsNone(chunk.fetch_key)
        self.assertIsNone(chunk.fetch_text)

    def test_overflowed_line_never_groups_and_discards_parser_state(self):
        limit = DIAGNOSTIC_LINE_LIMIT_BYTES
        padding = b"x" * (limit + 10)
        payload = (
            b"npm http fetch GET 200 https://example.com/"
            + padding
            + b" 15ms (cache miss)\n"
        )
        stream = self._stream()
        live = list(stream.feed_bytes(payload))
        finalized = [chunk for chunk in live if chunk.finalized]
        self.assertEqual(1, len(finalized))
        self.assertTrue(finalized[0].overflowed)
        self.assertIsNone(finalized[0].fetch_key)
        self.assertIsNone(finalized[0].fetch_text)
        # Parser state is discarded at the boundary: the next valid line groups.
        recovered = [
            chunk for chunk in stream.feed_bytes(self._RAW) if chunk.finalized
        ]
        self.assertEqual(1, len(recovered))
        self.assertIsNotNone(recovered[0].fetch_key)
        self.assertFalse(recovered[0].overflowed)
        stream.finish()

    def test_long_authority_groups_without_an_internal_length_limit(self):
        from docker.versioning.npm_fetch import parse_npm_fetch_line

        authority = "a" * 5_000 + ".example.com"
        url = f"https://{authority}/pkg.tgz"
        line = f"npm http fetch GET 200 {url} 15ms (cache miss)"
        # The authoritative complete-line parser accepts the long authority,
        # so the incremental collector must recognize and group it too.
        self.assertIsNotNone(parse_npm_fetch_line(line))
        for display in (
            NetworkUrlDisplay.REDACTED,
            NetworkUrlDisplay.HOST_PATH,
        ):
            with self.subTest(display=display):
                stream, _chunks, chunk = self._finalize(
                    line.encode() + b"\n", display=display, chunk_size=1
                )
                self.assertIsNotNone(chunk.fetch_key)
                self.assertIsNotNone(chunk.fetch_text)
                self.assertNotIn("https://", stream.tail())
                self.assertNotIn(url, stream.tail())

    def test_host_path_finalized_text_matches_prefixes_and_tail(self):
        stream, chunks, chunk = self._finalize(
            self._RAW, display=NetworkUrlDisplay.HOST_PATH, chunk_size=3
        )
        prefixes = "".join(
            stream_chunk.local_text or ""
            for stream_chunk in chunks
            if not stream_chunk.finalized
            and stream_chunk.local_text is not None
        )
        self.assertEqual(chunk.local_text, prefixes)
        self.assertEqual(chunk.local_text + "\n", stream.tail())

    def test_fail_closed_across_newline_clean_eof_and_abort(self):
        # A record newline resolves a trailing partial secret as ordinary text.
        _stream, _chunks, chunk = self._finalize(
            b"hello sec\n",
            secrets=("secret",),
            display=NetworkUrlDisplay.HOST_PATH,
        )
        self.assertEqual("hello sec", chunk.local_text)
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, chunk.local_text)
        # Clean EOF fails closed for the same partial secret.
        _stream, _chunks, chunk = self._finalize(
            b"hello sec",
            secrets=("secret",),
            display=NetworkUrlDisplay.HOST_PATH,
        )
        self.assertEqual(f"hello {INCOMPLETE_TOKEN_MARKER}", chunk.local_text)
        # Aborted EOF keeps a complete URL fail-closed.
        stream = self._stream(display=NetworkUrlDisplay.HOST_PATH)
        stream.feed_bytes(b"hello https://example.com/path")
        finalized = [
            chunk for chunk in stream.finish(abort=True) if chunk.finalized
        ]
        self.assertEqual(1, len(finalized))
        self.assertIn(INCOMPLETE_TOKEN_MARKER, finalized[0].local_text)

    def test_exact_retains_selected_source_and_never_groups(self):
        stream, _chunks, chunk = self._finalize(
            self._RAW, display=NetworkUrlDisplay.EXACT
        )
        self.assertIsNone(chunk.fetch_key)
        self.assertIsNone(chunk.fetch_text)
        self.assertEqual(self._LINE, chunk.local_text)
        # ``exact`` is the one mode allowed to retain terminal-safe source.
        self.assertEqual(self._LINE + "\n", stream.tail())
        self.assertIn(self._URL, stream.tail())


if __name__ == "__main__":
    unittest.main()
