"""Source-coordinate projection coordinator (internal).

The deque is the sole retained raw owner. Recognition is fed once per source
symbol and never rebuilt, including commits and overflow recovery. Display,
raw ownership, component settlement and validated metadata have independent
frontiers. At fixed registration, ordinary lookahead is bounded by scheme /
secret prefixes; URL materialization occurs only at settled component or token
boundaries, never on every growing feed. The 8 KiB budget covers the entire
raw deque, not a read size. Counters are stream-lifetime and never reset.

An earlier unresolved literal prefix crossing a recognized separator must
withhold source-order-dependent redacted delivery. Clean EOF resolves that
prefix as a non-match and releases the ordinary preceding text followed by
one URL marker; abort/overflow retain their fail-closed rules. This settlement
never changes the original-source validation and secret-coverage gates for facts.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import islice
import codecs
from typing import TYPE_CHECKING

from docker.versioning.diagnostic_identity import SessionUrlIdentity

if TYPE_CHECKING:
    from docker.versioning.diagnostic_projection import SafeHostPath

from docker.versioning.diagnostic_recognition import SUPPORTED_SCHEMES
from docker.versioning.diagnostic_recognizer import IncrementalRecognizer
from docker.versioning.model import NetworkUrlDisplay


@dataclass(frozen=True, slots=True)
class ProjectionWork:
    recognition_instances: int
    encoded_bytes: int
    buffer_appends: int
    buffer_pops: int
    materialized_chars: int
    provenance_visits: int


@dataclass(slots=True)
class _Url:
    start: int
    detected: int
    eligible: bool
    end: int | None = None
    authority_end: int | None = None
    path_end: int | None = None
    host_done: bool = False
    path_done: bool = False
    host: str | None = None
    masked_host: bool = False
    path: str | None = None
    depth: int = 0
    component_secrets: tuple[str, ...] | None = None


class CoordinatedProjection:
    """Implementation mixin; the boundary module owns validation helpers."""

    _secrets: tuple[str, ...]
    _hostnames: list[str]
    _host_paths: list[SafeHostPath]
    _host_path_displays: list[SafeHostPath]
    _fingerprints: list[str]
    _url_identity: SessionUrlIdentity | None
    _decoder: codecs.IncrementalDecoder

    def _init_coordination(
        self, url_display: NetworkUrlDisplay, capture_redacted: bool = False
    ) -> None:
        if not isinstance(url_display, NetworkUrlDisplay) or url_display not in (
            NetworkUrlDisplay.REDACTED,
            NetworkUrlDisplay.HOST_PATH,
        ):
            raise ValueError("projector requires a safe NetworkUrlDisplay")
        self._url_display = url_display
        self._redacted_chunks: list[str] | None = [] if capture_redacted else None
        self._recognizer = IncrementalRecognizer(self._secrets)
        self._buffer: deque[tuple[str, int]] = deque()
        self._buffer_start = 0
        self._pending_bytes = 0
        self._url: _Url | None = None
        self._overflow = False
        self._last_secret_end: int | None = None
        self._selected_secret_start: int | None = None
        self._selected_secret_end = 0
        self._host_path_displays = []
        self._encoded_bytes = 0
        self._appends = 0
        self._pops = 0
        self._materialized = 0
        self._provenance_visits = 0

    @property
    def _pending(self) -> str:
        """Inspection-only snapshot; production draining does not use it."""
        return "".join(character for character, _ in self._buffer)

    @property
    def recognition_stats(self):
        return self._recognizer.stats

    @property
    def work_stats(self) -> ProjectionWork:
        return ProjectionWork(
            1,
            self._encoded_bytes,
            self._appends,
            self._pops,
            self._materialized,
            self._provenance_visits,
        )

    @property
    def pending_size(self) -> int:
        return self._pending_bytes

    def take_redacted_chunks(self) -> tuple[str, ...]:
        """Drain the transient SDK product, never a second raw/retained tail."""
        if not self._redacted_chunks:
            return ()
        result = ("".join(self._redacted_chunks),)
        self._redacted_chunks.clear()
        return result

    def _safe_emit(self, text: str) -> None:
        if self._redacted_chunks is not None:
            self._redacted_chunks.append(text)

    def _both(self, out: list[str], text: str) -> None:
        out.append(text)
        self._safe_emit(text)

    def discard_pending(self) -> None:
        """Abandon bounded metadata/display remainder at line overflow.

        The line marker is an independent synthetic product, not source to
        recognize. Continue existing cursors through recovery, without replay.
        """
        self._commit(self._recognizer.consumed)
        self._url = None
        self._overflow = True

    def _source(self, end: int | None = None) -> str:
        size = len(self._buffer) if end is None else max(0, end - self._buffer_start)
        self._materialized += min(size, len(self._buffer))
        return "".join(character for character, _ in islice(self._buffer, size))

    def _commit(self, end: int) -> None:
        end = min(end, self._recognizer.consumed)
        while self._buffer and self._buffer_start < end:
            _, size = self._buffer.popleft()
            self._pending_bytes -= size
            self._buffer_start += 1
            self._pops += 1
        if not self._buffer:
            self._buffer_start = max(self._buffer_start, end)
        self._recognizer.consume(end)

    def _feed(self, text: str) -> tuple[str, ...]:
        from docker.versioning.diagnostic_projection import (
            PENDING_LIMIT_BYTES,
            _is_terminator,
        )

        if self._redacted_chunks is not None:
            self._redacted_chunks.clear()
        out = []
        for character in text:
            size = len(character.encode("utf-8"))
            self._encoded_bytes += size
            if not self._overflow and self._pending_bytes + size > PENDING_LIMIT_BYTES:
                out.extend(self._begin_overflow())
            self._recognizer.feed(character)
            if self._overflow:
                self._recognizer.consume(self._recognizer.consumed)
                self._buffer_start = self._recognizer.consumed
                if not _is_terminator(character):
                    continue
                self._overflow = False
                self._buffer_start = self._recognizer.consumed - 1
            self._buffer.append((character, size))
            self._pending_bytes += size
            self._appends += 1
            if self._url is not None and self._url.end is None:
                if _is_terminator(character):
                    self._url.end = self._recognizer.consumed - 1
                else:
                    for depth, event in enumerate(self._recognizer.structural_events):
                        if (
                            depth == self._url.depth
                            and event is not None
                            and event[1] >= self._url.detected
                        ):
                            symbol, start, _ = event
                            if self._url.authority_end is None:
                                self._url.authority_end = start
                            if symbol in "?#" and self._url.path_end is None:
                                self._url.path_end = start
            out.extend(self._drain_coordinated())
        return ("".join(out),) if out else ()

    def _begin_overflow(self) -> list[str]:
        from docker.versioning.diagnostic_projection import OVERSIZED_TOKEN_MARKER

        out = []
        if self._url is None or (
            self._url_display is NetworkUrlDisplay.HOST_PATH and not self._url.path_done
        ):
            out.append(OVERSIZED_TOKEN_MARKER)
            if self._url is None:
                self._safe_emit(OVERSIZED_TOKEN_MARKER)
        self._commit(self._recognizer.consumed)
        self._url = None
        self._overflow = True
        return out

    def _settled(self, boundary: int, *, final: bool, decoded: bool = False) -> bool:
        start = self._recognizer.secret_prefix_start(decoded=decoded)
        return final or start is None or start >= boundary

    def _coverage(self, start: int, end: int) -> bool:
        if start >= end:
            return False
        return any(
            match.start < end and match.end > start
            for match in self._recognizer.secret_records(decoded=True)
        )

    def _settle_components(
        self, *, final: bool = False, abort: bool = False
    ) -> list[str]:
        from docker.versioning.diagnostic_projection import (
            INCOMPLETE_TOKEN_MARKER,
            REDACTED_PATH_MARKER,
            SafeHostPath,
            _candidate_url_layer,
            normalized_url_host,
            _host_contains_secret,
            _safe_host_path,
        )
        import urllib.parse

        url = self._url
        assert url is not None
        secrets = (
            self._secrets if url.component_secrets is None else url.component_secrets
        )
        if self._url_display is NetworkUrlDisplay.REDACTED or url.path_done:
            return []
        prefix = self._recognizer.secret_prefix_start(decoded=True) if final else None
        if prefix is not None and any(
            match.start <= prefix and match.end >= self._recognizer.consumed
            for match in self._recognizer.secret_records(decoded=True)
        ):
            prefix = None
        if abort or (
            prefix is not None
            and prefix
            < (
                url.path_end
                if url.path_end is not None
                else url.end
                if url.end is not None
                else self._recognizer.consumed
            )
        ):
            url.path_done = True
            return [INCOMPLETE_TOKEN_MARKER]
        boundary = url.authority_end if url.authority_end is not None else url.end
        if boundary is None:
            return []
        out = []
        if not url.host_done and self._settled(boundary, final=final, decoded=True):
            source = self._source(url.end)
            layer = _candidate_url_layer(source) if url.eligible else None
            host = normalized_url_host(layer) if layer is not None else None
            if host is None:
                url.host_done = url.path_done = True
                return ["<redacted>"]
            url.host = host
            raw_host_start = self._raw_host_start(source, layer, url, boundary)
            url.masked_host = _host_contains_secret(host, secrets) or self._coverage(
                raw_host_start, boundary
            )
            if url.masked_host:
                url.host = "<redacted-host>"
            url.host_done = True
            out.append(url.host)
        path_end = url.path_end if url.path_end is not None else url.end
        if (
            url.host_done
            and path_end is not None
            and self._settled(path_end, final=final, decoded=True)
        ):
            source = self._source(url.end)
            layer = _candidate_url_layer(source)
            if layer is None or url.host is None:
                url.path = REDACTED_PATH_MARKER
            else:
                url.path = _safe_host_path(layer, url.host, secrets).path
                parsed = urllib.parse.urlsplit(layer)
                if parsed.path and self._coverage(boundary, path_end):
                    url.path = REDACTED_PATH_MARKER
            url.path_done = True
            out.append(url.path)
            if url.host is not None:
                display = SafeHostPath(url.host, url.path)
                if display not in self._host_path_displays:
                    self._host_path_displays.append(display)
        return out

    def _raw_host_start(
        self, source: str, layer: str | None, url: _Url, boundary: int
    ) -> int:
        """Translate userinfo's final '@' without an input-sized source map.

        Interpretation is bounded to three passes. This is component
        validation work over a settled snapshot, never recognition replay.
        The temporary decoder units are bounded, and their visits are counted.
        """
        from docker.versioning.diagnostic_projection import _decode_percent
        from docker.versioning.diagnostic_recognizer import _Transducer, _MutableStats

        view = source
        depth = 0
        while view != layer and depth < 3:
            decoded = _decode_percent(view)
            if decoded is None or decoded == view:
                break
            view = decoded
            depth += 1
        start = url.detected

        def observe(character, source_start, source_end, from_token):
            nonlocal start
            if character == "@" and source_end <= boundary:
                start = source_end

        stats = _MutableStats()
        sink = observe
        decoders = []
        for _ in range(depth):
            decoder = _Transducer(sink, stats)
            decoders.insert(0, decoder)
            sink = decoder.feed
        for index, character in enumerate(source):
            raw = self._buffer_start + index
            sink(character, raw, raw + 1, True)
        for decoder in decoders:
            decoder.flush()
        self._provenance_visits += len(source) + stats.emissions
        return start

    def _record_url(self, source: str, url: _Url) -> None:
        from docker.versioning.diagnostic_projection import (
            REDACTED_PATH_MARKER,
            SafeHostPath,
            _candidate_url_layer,
            normalized_url_host,
            _host_contains_secret,
            _safe_host_path,
            _canonical_url_identity,
        )

        if not url.eligible:
            return
        if self._eof and url.end == self._recognizer.consumed:
            from docker.versioning.diagnostic_projection import (
                _decode_percent,
                _is_escape_complete,
                _is_complete_url_literal,
            )

            view = source
            complete = False
            for _ in range(4):
                if _is_escape_complete(view) and _is_complete_url_literal(view):
                    complete = True
                    break
                decoded = _decode_percent(view)
                if decoded is None or decoded == view:
                    break
                view = decoded
            if not complete:
                return
        layer = _candidate_url_layer(source)
        host = normalized_url_host(layer) if layer is not None else None
        if layer is None or host is None:
            return
        # Facts are granted only at token completion, never at display commit.
        boundary = url.authority_end if url.authority_end is not None else url.end
        assert boundary is not None and url.end is not None
        host_start = self._raw_host_start(source, layer, url, boundary)
        secrets = (
            self._secrets if url.component_secrets is None else url.component_secrets
        )
        if _host_contains_secret(host, secrets) or self._coverage(host_start, boundary):
            return
        if host not in self._hostnames:
            self._hostnames.append(host)
        fact = _safe_host_path(layer, host, secrets)
        if self._coverage(
            boundary, url.path_end if url.path_end is not None else url.end
        ):
            fact = SafeHostPath(host, REDACTED_PATH_MARKER)
        if fact not in self._host_paths:
            self._host_paths.append(fact)
        if self._url_identity is not None and fact.path != REDACTED_PATH_MARKER:
            identity = _canonical_url_identity(source)
            if identity is not None:
                self._fingerprints.append(self._url_identity.fingerprint(identity))

    def _drain_coordinated(
        self, *, final: bool = False, abort: bool = False
    ) -> list[str]:
        from docker.versioning.diagnostic_projection import (
            INCOMPLETE_TOKEN_MARKER,
            _find_terminator,
        )

        out = []
        while self._buffer:
            if self._url is not None:
                url = self._url
                out.extend(self._settle_components(final=final, abort=abort))
                if url.end is None or not self._settled(url.end, final=final):
                    break
                if not abort:
                    self._record_url(self._source(url.end), url)
                # A literal secret may cross the raw URL end: its outside
                # suffix is covered by the URL marker, never another marker.
                end = url.end
                for match in self._recognizer.secret_records():
                    if match.start < url.end and match.end > end:
                        end = match.end
                self._commit(end)
                self._url = None
                continue
            match = self._recognizer.earliest_match()
            secret = min(
                (
                    record
                    for record in self._recognizer.secret_records()
                    if record.start >= self._selected_secret_end
                    or record.start == self._selected_secret_start
                ),
                key=lambda record: (record.start, -record.end, record.pattern_index),
                default=None,
            )
            prefix = self._recognizer.secret_prefix_start(
                minimum_start=self._selected_secret_end,
                include_start=self._selected_secret_start,
            )
            if (
                final
                and not abort
                and match is not None
                and prefix is not None
                and prefix < match.start
            ):
                # A literal prefix reaching past an already detected separator
                # cannot complete at clean EOF. Unlike a standalone ambiguous
                # secret, it resolves as ordinary preceding source plus URL
                # coverage. Existing completed secret records still apply.
                prefix = None
            if secret is not None and prefix is not None and prefix < secret.start:
                if final:
                    self._both(out, INCOMPLETE_TOKEN_MARKER)
                    self._commit(self._recognizer.consumed)
                    break
                if prefix > self._buffer_start:
                    self._both(out, self._source(prefix))
                    self._commit(prefix)
                break
            if (
                match is not None
                and prefix is not None
                and prefix < match.start
                and secret is None
            ):
                if prefix > self._buffer_start:
                    self._both(out, self._source(prefix))
                    self._commit(prefix)
                if not final:
                    break
                self._both(out, INCOMPLETE_TOKEN_MARKER)
                self._last_secret_end = match.start
                self._commit(match.start)
            if secret is not None and (match is None or secret.start < match.start):
                if secret.start > self._buffer_start:
                    self._both(out, self._source(secret.start))
                    self._commit(secret.start)
                candidate_end = None
                if match is not None:
                    source = self._source()
                    relative = _find_terminator(
                        source, max(0, match.start - self._buffer_start)
                    )
                    candidate_end = (
                        None if relative is None else self._buffer_start + relative
                    )
                if secret.start >= self._buffer_start:
                    self._both(out, "<secret>")
                    self._selected_secret_start = secret.start
                self._selected_secret_end = max(self._selected_secret_end, secret.end)
                from docker.versioning.diagnostic_projection import _is_terminator

                self._last_secret_end = (
                    None
                    if _is_terminator(self._secrets[secret.pattern_index][-1])
                    else secret.end
                )
                self._commit(secret.end)
                if match is None or (
                    candidate_end is not None and candidate_end <= secret.end
                ):
                    continue
            if match is not None:
                component_secrets = self._secrets
                if match.start > self._buffer_start:
                    # The previous feed withheld an earlier literal prefix.
                    # At mismatch (or clean EOF), it is not a secret-bearing
                    # component merely because its registered continuation
                    # contains this hostname/path as a substring. Preserve
                    # all other patterns and actual coordinate coverage.
                    source = self._source()
                    previous = source if final and not abort else source[:-1]
                    if len(previous) >= match.separator_end - self._buffer_start:
                        component_secrets = tuple(
                            secret
                            for secret in self._secrets
                            if not secret.startswith(previous)
                        )
                    self._both(out, self._source(match.start))
                    self._commit(match.start)
                eligible = match.start == self._buffer_start and (
                    self._last_secret_end is None or self._last_secret_end < match.start
                )
                self._url = _Url(
                    match.start,
                    match.separator_end,
                    eligible,
                    component_secrets=component_secrets,
                )
                if eligible:
                    from docker.versioning.diagnostic_projection import _decode_percent

                    view = self._source()
                    for depth in range(4):
                        if (
                            view.partition("://")[0].lower() in SUPPORTED_SCHEMES
                            and "://" in view
                        ):
                            self._url.depth = depth
                            break
                        decoded = _decode_percent(view)
                        if decoded is None or decoded == view:
                            break
                        view = decoded
                self._safe_emit("<redacted>")
                if self._url_display is NetworkUrlDisplay.REDACTED:
                    out.append("<redacted>")
                # For a delayed candidate exposed after a secret settles,
                # recover component/token boundaries once from owned source.
                source = self._source()
                boundary = _find_terminator(source)
                if boundary is not None:
                    self._url.end = self._buffer_start + boundary
                elif final:
                    self._url.end = self._recognizer.consumed
                if eligible:
                    self._recover_component_boundaries(source, self._url)
                out.extend(self._settle_components(final=final, abort=abort))
                if final:
                    continue
                break
            url_start = (
                self._recognizer.retain_from()
                if self._recognizer.prefix_state()
                else None
            )
            holds = [start for start in (prefix, url_start) if start is not None]
            hold = (
                max(self._buffer_start, min(holds))
                if holds
                else self._recognizer.consumed
            )
            if final:
                source = self._source()
                sensitive = prefix is not None or (
                    url_start is not None and (abort or self._entered_separator(source))
                )
                self._both(out, INCOMPLETE_TOKEN_MARKER if sensitive else source)
                self._commit(self._recognizer.consumed)
                break
            if hold > self._buffer_start:
                self._both(out, self._source(hold))
                self._commit(hold)
            break
        return out

    def _recover_component_boundaries(self, source: str, url: _Url) -> None:
        """Inspect a settled activation snapshot, not a recognition replay.

        Delayed activation can occur after authority/path boundaries. Decode
        only the selected interpretation depth, carrying bounded provenance
        units so boundaries remain absolute raw coordinates. The actual
        separator end comes from recognition, never the activation offset.
        """
        from docker.versioning.diagnostic_recognizer import _Transducer, _MutableStats

        stats = _MutableStats()

        def observe(character, start, end, from_token):
            if start < url.detected or (url.end is not None and start >= url.end):
                return
            if character in "/?#" and url.authority_end is None:
                url.authority_end = start
            if character in "?#" and url.path_end is None:
                url.path_end = start

        sink = observe
        decoders = []
        for _ in range(url.depth):
            decoder = _Transducer(sink, stats)
            decoders.insert(0, decoder)
            sink = decoder.feed
        for index, character in enumerate(source):
            raw = self._buffer_start + index
            sink(character, raw, raw + 1, True)
        for decoder in decoders:
            decoder.flush()
        self._provenance_visits += len(source) + stats.emissions

    @staticmethod
    def _entered_separator(source: str) -> bool:
        from docker.versioning.diagnostic_projection import _decode_percent

        for _ in range(4):
            if ":" in source or "/" in source:
                return True
            decoded = _decode_percent(source)
            if decoded is None or decoded == source:
                break
            source = decoded
        return False

    def finish(self, *, abort: bool = False) -> tuple[str, ...]:
        if self._eof:
            return ()
        out = list(self._feed(self._decoder.decode(b"", final=True)))
        self._eof = True
        self._recognizer.finalize()
        if self._overflow:
            self._overflow = False
            return tuple(out)
        if self._url is not None and self._url.end is None:
            self._url.end = self._recognizer.consumed
        out.extend(self._drain_coordinated(final=True, abort=abort))
        return tuple(out)
