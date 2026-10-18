"""Phase 5 RED tests: consumer channels, grouping and fetch display.

These bind tasks 5.1--5.3 of ``linearize-diagnostic-url-detection`` against the
revised supported-scheme and secret-aware policy.  The collector's three
selected representations -- the URL-free structured/SDK ``text``, the
mode-selected local ``local_text``, and the single retained ``tail`` -- must
agree with the policy, and a recognized successful fetch must render its
``host-path`` group key and canonical text with the same masked products the
rest of the diagnostic path uses (``<redacted-host>`` / ``/<redacted-path>``).

The channel-isolation obligations are already implemented by earlier phases and
are asserted here so a regression cannot silently disable them.
"""

from __future__ import annotations

import unittest

from docker.npm_environment.streaming import REDACTED
from docker.versioning.diagnostic_projection import (
    REDACTED_PATH_MARKER,
    sanitize_host_paths,
)
from docker.versioning.host_presentation import (
    HostPresentationMode,
    HostPresentationState,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticEnvelope,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_diagnostic_stream import NpmDiagnosticStream
from docker.versioning.npm_fetch import (
    canonical_fetch_text,
    fetch_group_identity,
    parse_npm_fetch_line,
)

REDACTED_HOST = "<redacted-host>"
REQUEST_SEPARATOR = " — "

FETCH_PREFIX = "npm http fetch GET 200"


def _finalized(text: str, *, secrets=(), mode=NetworkUrlDisplay.REDACTED):
    """Return ``(all_chunks, finalized_chunks, tail)`` for one source line."""
    stream = NpmDiagnosticStream("stdout", secrets, network_url_display=mode)
    chunks = list(stream.feed_bytes(text.encode("utf-8")))
    chunks.extend(stream.finish())
    finalized = [chunk for chunk in chunks if chunk.finalized]
    return chunks, finalized, stream.tail()


def _only_finalized(text: str, *, secrets=(), mode=NetworkUrlDisplay.REDACTED):
    _, finalized, tail = _finalized(text, secrets=secrets, mode=mode)
    assert len(finalized) == 1, finalized
    return finalized[0], tail


def _sdk_text(chunk) -> str:
    """The external structured text the SDK sink would receive."""
    diagnostic = HostStructuredDiagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream(chunk.stream),
        classification=HostDiagnosticClassification.STATUS,
        text=chunk.text,
        hostnames=chunk.hostnames,
        url_fingerprints=chunk.url_fingerprints,
    )
    return diagnostic.text


def _fetch_line(url: str, *, latency: int = 15, cache: str = "miss") -> str:
    return f"{FETCH_PREFIX} {url} {latency}ms (cache {cache})"


def _plain_envelope(text: str) -> HostDiagnosticEnvelope:
    return HostDiagnosticEnvelope.for_diagnostic(
        HostStructuredDiagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text=text,
        )
    )


def _fetch_envelope(
    line: str, display: NetworkUrlDisplay, *, secrets=()
) -> HostDiagnosticEnvelope:
    record = parse_npm_fetch_line(line)
    assert record is not None, line
    key = fetch_group_identity(record, display, secrets=secrets)
    canonical = canonical_fetch_text(record, display, secrets=secrets)
    assert key is not None and canonical is not None
    diagnostic = HostStructuredDiagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream.STDOUT,
        classification=HostDiagnosticClassification.STATUS,
        text=canonical,
    )
    return HostDiagnosticEnvelope.for_diagnostic(
        diagnostic, text=canonical, fetch_key=key, fetch_text=canonical
    )


class RecordingRenderer:
    """Minimal terminal renderer recording the ordered call sequence."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def set_status(self, text: str) -> None:
        self.calls.append(("status", text))

    def set_slot(self, text: str) -> None:
        self.calls.append(("slot", text))

    def clear_slot(self) -> None:
        self.calls.append(("clear_slot"))

    def clear_status(self) -> None:
        self.calls.append(("clear_status"))

    def clear_all(self) -> None:
        self.calls.append(("clear_all"))

    def durable(self, text: str) -> None:
        self.calls.append(("durable", text))

    def finalize_diagnostic(self, text: str, *, restore_status: str | None) -> None:
        self.calls.append(("finalize", text))

    def slot_texts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "slot"]  # type: ignore[misc]

    def durable_texts(self) -> list[str]:
        return [call[1] for call in self.calls if call[0] == "durable"]  # type: ignore[misc]


class TestCollectorChannelModes(unittest.TestCase):
    """5.1 -- all-mode collector/tail with and without SDK delivery."""

    def test_redacted_hides_supported_url_in_all_channels(self):
        chunk, tail = _only_finalized(
            "npm error at https://registry.example.com/private done\n"
        )
        self.assertEqual("npm error at <redacted> done", chunk.text)
        self.assertEqual("npm error at <redacted> done", chunk.local_text)
        self.assertEqual("npm error at <redacted> done\n", tail)
        self.assertEqual(chunk.text, _sdk_text(chunk))

    def test_host_path_masks_secret_host_in_all_channels(self):
        line = _fetch_line("https://TOKEN.example/public") + "\n"
        chunk, tail = _only_finalized(
            line, secrets=("TOKEN",), mode=NetworkUrlDisplay.HOST_PATH
        )
        # SDK/structured text stays fully URL-free.
        self.assertNotIn("TOKEN", chunk.text)
        self.assertIn(REDACTED, chunk.text)
        # The local representation and the single retained tail already carry
        # the masked host.
        self.assertIn(REDACTED_HOST, chunk.local_text)
        self.assertIn("public", chunk.local_text)
        self.assertNotIn("TOKEN", tail)
        self.assertIn(f"{REDACTED_HOST}/public", tail)
        self.assertEqual((), chunk.hostnames)
        self.assertEqual((), chunk.url_fingerprints)
        # The fetch canonical text and group key must use the same masked
        # product as local/tail rather than falling back to ``<redacted>``.
        self.assertEqual(
            f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)",
            chunk.fetch_text,
        )
        self.assertEqual(f"{REDACTED_HOST}/public", chunk.fetch_key.host_path)

    def test_host_path_masks_secret_path_but_keeps_safe_host(self):
        line = _fetch_line("https://host.example/TOKEN/private") + "\n"
        chunk, tail = _only_finalized(
            line, secrets=("TOKEN",), mode=NetworkUrlDisplay.HOST_PATH
        )
        self.assertIn(REDACTED, chunk.text)
        self.assertIn(f"host.example{REDACTED_PATH_MARKER}", chunk.local_text)
        self.assertIn(f"host.example{REDACTED_PATH_MARKER}", tail)
        self.assertEqual(("host.example",), chunk.hostnames)
        self.assertEqual((), chunk.url_fingerprints)
        self.assertEqual(
            f"{FETCH_PREFIX} host.example{REDACTED_PATH_MARKER} (cache miss)",
            chunk.fetch_text,
        )
        self.assertEqual(
            f"host.example{REDACTED_PATH_MARKER}", chunk.fetch_key.host_path
        )

    def test_exact_keeps_local_source_and_redacts_sdk(self):
        line = _fetch_line("https://TOKEN.example/TOKEN/private") + "\n"
        chunk, tail = _only_finalized(
            line, secrets=("TOKEN",), mode=NetworkUrlDisplay.EXACT
        )
        # Local and the single retained tail preserve safe source.
        self.assertIn("TOKEN", chunk.local_text)
        self.assertIn("TOKEN", tail)
        # The structured/SDK text hides the supported URL and the secret.
        self.assertIn(REDACTED, chunk.text)
        self.assertNotIn("TOKEN", chunk.text)
        self.assertEqual(chunk.text, _sdk_text(chunk))

    def test_standalone_secret_is_marker_in_safe_modes(self):
        for mode in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(mode=mode):
                chunk, tail = _only_finalized(
                    "before TOKEN after\n", secrets=("TOKEN",), mode=mode
                )
                self.assertEqual("before <secret> after", chunk.text)
                self.assertEqual("before <secret> after", chunk.local_text)
                self.assertIn("before <secret> after", tail)

    def test_unknown_scheme_text_is_not_hidden(self):
        text = "loading custom+transport://internal.example/private now\n"
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                chunk, tail = _only_finalized(text, mode=mode)
                self.assertIn(
                    "custom+transport://internal.example/private", chunk.local_text
                )
                self.assertIn("custom+transport://internal.example/private", tail)
                self.assertEqual((), chunk.hostnames)
                self.assertEqual((), chunk.url_fingerprints)

    def test_single_selected_retained_representation(self):
        line = _fetch_line("https://TOKEN.example/public") + "\n"
        for mode, forbidden in (
            (NetworkUrlDisplay.REDACTED, "TOKEN"),
            (NetworkUrlDisplay.HOST_PATH, REDACTED_HOST),
        ):
            with self.subTest(mode=mode):
                chunks, finalized, tail = _finalized(
                    line, secrets=("TOKEN",), mode=mode
                )
                self.assertEqual(1, len(finalized))
                selected = "".join(
                    chunk.local_text
                    for chunk in finalized
                    if chunk.local_text is not None
                )
                # The tail is the finalized selected representation exactly
                # once, never a second (source or alternate-policy) tail.
                self.assertEqual(selected + "\n", tail)
                if mode is NetworkUrlDisplay.HOST_PATH:
                    self.assertIn(forbidden, tail)
                else:
                    self.assertNotIn(forbidden, tail)

    def test_masked_host_display_is_partition_invariant(self):
        line = _fetch_line("https://TOKEN.example/public") + "\n"
        data = line.encode("utf-8")
        whole, whole_tail = _only_finalized(
            line, secrets=("TOKEN",), mode=NetworkUrlDisplay.HOST_PATH
        )
        stream = NpmDiagnosticStream(
            "stdout", ("TOKEN",), network_url_display=NetworkUrlDisplay.HOST_PATH
        )
        chunks = []
        for index in range(len(data)):
            chunks.extend(stream.feed_bytes(data[index : index + 1]))
        chunks.extend(stream.finish())
        finalized = [chunk for chunk in chunks if chunk.finalized]
        self.assertEqual(1, len(finalized))
        self.assertEqual(whole.fetch_text, finalized[0].fetch_text)
        self.assertEqual(whole.fetch_key, finalized[0].fetch_key)
        self.assertEqual(whole_tail, stream.tail())

    def test_sdk_projection_is_independent_of_local_mode(self):
        line = _fetch_line("https://TOKEN.example/TOKEN/private") + "\n"
        sdk_texts = set()
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                chunk, _ = _only_finalized(line, secrets=("TOKEN",), mode=mode)
                self.assertEqual(chunk.text, _sdk_text(chunk))
                sdk_texts.add(_sdk_text(chunk))
        self.assertEqual(1, len(sdk_texts))
        self.assertNotIn("TOKEN", sdk_texts.pop())


class TestGroupingPolicy(unittest.TestCase):
    """5.2 -- ordinary/fetch grouping under masks and eligible identities."""

    def test_unknown_rendered_text_groups_by_rendered_text(self):
        text = "custom+transport://a.example/x"
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.LINES,
            network_url_display=NetworkUrlDisplay.REDACTED,
        )
        for index in range(2):
            state.admit_diagnostic(_plain_envelope(text), now=float(index) * 0.1)
        state.due(now=1.0)
        # Identical unknown-scheme text groups by its rendered text, with no
        # URL-derived identity.
        self.assertEqual([text, f"{text} (repeated 2 times)"], renderer.durable_texts())
        self.assertEqual((), _plain_envelope(text).url_fingerprints)

        # Different unknown-scheme text does not group.
        other = RecordingRenderer()
        other_state = HostPresentationState(
            other,
            mode=HostPresentationMode.LINES,
            network_url_display=NetworkUrlDisplay.REDACTED,
        )
        other_state.admit_diagnostic(_plain_envelope(text), now=0.0)
        other_state.admit_diagnostic(
            _plain_envelope("custom+transport://b.example/y"), now=0.1
        )
        other_state.due(now=1.0)
        self.assertEqual(
            [text, "custom+transport://b.example/y"], other.durable_texts()
        )

    def test_secret_bearing_fetches_share_the_masked_group(self):
        lines = (
            _fetch_line("https://TOKEN-a.example/public", latency=15),
            _fetch_line("https://TOKEN-b.example/public", latency=25),
        )
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.INTERACTIVE,
            network_url_display=NetworkUrlDisplay.HOST_PATH,
        )
        for index, line in enumerate(lines):
            state.admit_diagnostic(
                _fetch_envelope(line, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)),
                now=float(index) * 0.1,
            )
        # The hidden hosts must not split the group.
        self.assertEqual(
            [
                f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)",
                f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)"
                f"{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.slot_texts(),
        )

    def test_secret_bearing_paths_share_the_masked_group(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.INTERACTIVE,
            network_url_display=NetworkUrlDisplay.HOST_PATH,
        )
        for index, line in enumerate(
            (
                _fetch_line("https://host.example/TOKEN-a/private", latency=15),
                _fetch_line("https://host.example/TOKEN-b/private", latency=25),
            )
        ):
            state.admit_diagnostic(
                _fetch_envelope(line, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)),
                now=float(index) * 0.1,
            )
        self.assertEqual(
            [
                f"{FETCH_PREFIX} host.example{REDACTED_PATH_MARKER} (cache miss)",
                f"{FETCH_PREFIX} host.example{REDACTED_PATH_MARKER} (cache miss)"
                f"{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.slot_texts(),
        )

    def test_eligible_supported_urls_still_distinguish_groups(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.INTERACTIVE,
            network_url_display=NetworkUrlDisplay.HOST_PATH,
        )
        state.admit_diagnostic(
            _fetch_envelope(
                _fetch_line("https://a.example/public"), NetworkUrlDisplay.HOST_PATH
            ),
            now=0.0,
        )
        state.admit_diagnostic(
            _fetch_envelope(
                _fetch_line("https://b.example/public"), NetworkUrlDisplay.HOST_PATH
            ),
            now=0.1,
        )
        self.assertEqual(
            [
                f"{FETCH_PREFIX} a.example/public (cache miss)",
                f"{FETCH_PREFIX} b.example/public (cache miss)",
            ],
            renderer.slot_texts(),
        )

    def test_lines_mode_uses_the_masked_group(self):
        renderer = RecordingRenderer()
        state = HostPresentationState(
            renderer,
            mode=HostPresentationMode.LINES,
            network_url_display=NetworkUrlDisplay.HOST_PATH,
        )
        for index, line in enumerate(
            (
                _fetch_line("https://TOKEN-a.example/public", latency=15),
                _fetch_line("https://TOKEN-b.example/public", latency=25),
            )
        ):
            state.admit_diagnostic(
                _fetch_envelope(line, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)),
                now=float(index) * 0.1,
            )
        state.due(now=1.0)
        self.assertEqual(
            [
                f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)",
                f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)"
                f"{REQUEST_SEPARATOR}2 requests",
            ],
            renderer.durable_texts(),
        )


class TestFetchSidePaths(unittest.TestCase):
    """5.3 -- supported/unknown/secret-bearing fetch side paths."""

    def test_supported_fetch_renders_normalized_host_and_path(self):
        record = parse_npm_fetch_line(_fetch_line("https://host.example/a/b"))
        assert record is not None
        self.assertEqual(
            f"{FETCH_PREFIX} host.example/a/b (cache miss)",
            canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH),
        )
        key = fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
        assert key is not None
        self.assertEqual("host.example/a/b", key.host_path)

    def test_latency_variants_share_a_supported_group(self):
        first = parse_npm_fetch_line(_fetch_line("https://host.example/a", latency=1))
        second = parse_npm_fetch_line(_fetch_line("https://host.example/a", latency=2))
        assert first is not None and second is not None
        self.assertEqual(
            fetch_group_identity(first, NetworkUrlDisplay.HOST_PATH),
            fetch_group_identity(second, NetworkUrlDisplay.HOST_PATH),
        )

    def test_unknown_scheme_is_recognized_without_grouping_fields(self):
        url = "custom+transport://internal.example/private"
        record = parse_npm_fetch_line(_fetch_line(url))
        # The fetch grammar recognizes any RFC-valid scheme...
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual((), sanitize_host_paths(url + " ", ()))
        # ...but it yields no URL-derived host/path group field and never
        # leaks the raw URL into the canonical text.
        key = fetch_group_identity(record, NetworkUrlDisplay.HOST_PATH)
        assert key is not None
        self.assertIsNone(key.host_path)
        canonical = canonical_fetch_text(record, NetworkUrlDisplay.HOST_PATH)
        assert canonical is not None
        self.assertNotIn("internal.example", canonical)
        self.assertIn(REDACTED, canonical)

    def test_secret_bearing_host_fetch_is_recognized_and_masked(self):
        line = _fetch_line("https://TOKEN.example/public")
        record = parse_npm_fetch_line(line)
        assert record is not None
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)
        )
        assert key is not None
        self.assertEqual(f"{REDACTED_HOST}/public", key.host_path)
        self.assertEqual(
            f"{FETCH_PREFIX} {REDACTED_HOST}/public (cache miss)",
            canonical_fetch_text(
                record, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)
            ),
        )

    def test_secret_bearing_path_fetch_is_recognized_and_masked(self):
        line = _fetch_line("https://host.example/TOKEN/private")
        record = parse_npm_fetch_line(line)
        assert record is not None
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)
        )
        assert key is not None
        self.assertEqual(f"host.example{REDACTED_PATH_MARKER}", key.host_path)

    def test_unknown_scheme_with_literal_secret_contributes_no_group_field(self):
        # A literal secret inside an unsupported URL is still redacted from the
        # text, but that ``<secret>`` substitution must never be mistaken for a
        # URL-derived host/path group field.
        url = "custom://user:TOKEN@internal.example/x"
        record = parse_npm_fetch_line(_fetch_line(url))
        self.assertIsNotNone(record)
        assert record is not None
        key = fetch_group_identity(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)
        )
        assert key is not None
        self.assertIsNone(key.host_path)
        canonical = canonical_fetch_text(
            record, NetworkUrlDisplay.HOST_PATH, secrets=("TOKEN",)
        )
        self.assertEqual(f"{FETCH_PREFIX} {REDACTED} (cache miss)", canonical)
        assert canonical is not None
        for leaked in ("TOKEN", "custom", "user", "internal.example"):
            self.assertNotIn(leaked, canonical)

    def test_unknown_scheme_secret_fetch_collector_agrees(self):
        # The streaming collector must agree with the complete-line parser: an
        # unsupported URL contributes no host/path group field and the secret
        # never reaches the structured, local, or canonical rendering.
        line = _fetch_line("custom://user:TOKEN@internal.example/x") + "\n"
        chunk, tail = _only_finalized(
            line, secrets=("TOKEN",), mode=NetworkUrlDisplay.HOST_PATH
        )
        self.assertIsNotNone(chunk.fetch_key)
        self.assertIsNone(chunk.fetch_key.host_path)
        self.assertNotIn("TOKEN", chunk.text)
        self.assertNotIn("TOKEN", chunk.local_text)
        self.assertNotIn("TOKEN", tail)
        self.assertNotIn("TOKEN", chunk.fetch_text)
        self.assertEqual(f"{FETCH_PREFIX} {REDACTED} (cache miss)", chunk.fetch_text)

    def test_parser_grammar_is_unchanged_for_supported_and_unknown(self):
        for url in (
            "https://host.example/a",
            "custom+transport://internal.example/private",
        ):
            with self.subTest(url=url):
                record = parse_npm_fetch_line(_fetch_line(url))
                self.assertIsNotNone(record)
                assert record is not None
                self.assertEqual(url, record.url)
        # Near-matches still reject conservatively.
        self.assertIsNone(parse_npm_fetch_line("npm http fetch GET 200"))
        self.assertIsNone(
            parse_npm_fetch_line("npm http cache x@https://host/a 0ms (cache hit)")
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
