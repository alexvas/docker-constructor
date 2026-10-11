"""P3 bounded partition corpus; not arbitrary suffix batch equivalence."""
import random

from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import DiagnosticProjector, PENDING_LIMIT_BYTES
from docker.versioning.model import NetworkUrlDisplay

# Share one ephemeral key only within this comparison run so partitioned
# fingerprints are comparable; no key or fingerprint enters the evidence log.
PARTITION_IDENTITY = SessionUrlIdentity()

CASES = (
    ("GET https://alice:s3cr3t@example.com:8443/path?q=1#frag done", ("s3cr3t",)),
    ("up https://user%3Apass@Example.com/a%20b?q=x%2Fy end", ()),
    ("a git://x.example/p b custom+transport://y.example/q c", ()),
    ("GET %68%74%74%70%73%3A%2F%2Fh.example/x done", ()),
    ("see https://bücher.example/x done", ()),
    ("note:/section and npm:package stay", ()),
    ("pre git post", ()),
    ("token=supersecret https://a.example/x next", ("supersecret",)),
    ("safe TOKENhttps://host end", ("TOKENht",)),
    ("https://host/password done after", ("password done",)),
    ("https://TOKEN.example/public end", ("TOKEN",)),
    ("https://host/TOKEN/private end", ("TOKEN",)),
    ("https://user:TOKEN@host/public end", ("TOKEN",)),
    ("https%2525253A%2525252F%2525252Fhost end", ()),
    ("%%25368ttps://host end", ("%",)),
    ("https://host/pa", ("host/path",)),
    ("%2525256", ()),
    ("Xhttps://host done", ("Xhttps://host",)),
    ("Xhttps://hor done", ("Xhttps://host",)),
    ("Xhttps://ho", ("Xhttps://host",)),
    ("Xhttps://ho", ("Xhttps://host", "ho")),
    ("Xhttps:/", ("Xhttps://host",)),
    ("Xhttps://host/path?q done", ("Xhttps://host/pathZ",)),
    ("Xhttps%3A%2F%2Fhost/path?q done", ("Xhttps%3A%2F%2Fhost/pathZ",)),
    ("Xhttps%253A%252F%252Fhost/path?q done", ("Xhttps%253A%252F%252Fhost/pathZ",)),
)


def project(text, secrets, mode, chunks):
    p = DiagnosticProjector(secrets, url_display=mode, url_identity=PARTITION_IDENTITY)
    out = []
    for chunk in chunks:
        out.extend(p.feed_bytes(chunk))
        assert p.pending_size <= PENDING_LIMIT_BYTES
    out.extend(p.finish())
    assert p.recognition_stats.chars == len(text)
    return "".join(out), p.hostnames, p.host_paths, p.url_fingerprints


def main():
    comparisons = 0
    for mode in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
        for text, secrets in CASES:
            data = text.encode("utf-8")
            baseline = project(text, secrets, mode, (data,))
            for split in range(len(data) + 1):
                assert project(text, secrets, mode, (data[:split], data[split:])) == baseline
                comparisons += 1
            assert project(text, secrets, mode, tuple(bytes((value,)) for value in data)) == baseline
            comparisons += 1
    rng = random.Random(20240607)
    alphabet = "abz019%:/. -+@#?htpscux&="
    for symbols, count, secrets in ((alphabet, 12000, ()),
                                     (alphabet, 10000, ("secret",)),
                                     ("nN%253A%252F%252F:./ https", 10000, ("https",))):
        for _ in range(count):
            text = "".join(rng.choice(symbols) for _ in range(rng.randint(0, 40)))
            data = text.encode()
            positions = sorted(rng.sample(range(len(data) + 1),
                                          min(len(data) + 1, rng.randint(1, 3))))
            start = 0
            chunks = []
            for end in (*positions, len(data)):
                chunks.append(data[start:end])
                start = end
            baseline = project(text, secrets, NetworkUrlDisplay.REDACTED, (data,))
            assert project(text, secrets, NetworkUrlDisplay.REDACTED, chunks) == baseline
            comparisons += 1
    print(f"P3 PARTITION CORPUS PASS: {comparisons} comparisons, text and ordered facts")


if __name__ == "__main__":
    main()
