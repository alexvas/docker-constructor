"""Owned read seam for green-gate fixture and repository contract inputs."""

from docker._owned_file import owned_file


def read_bytes(path):
    """One prepared OwnedStream is the sole release authority for each read."""
    with owned_file(lambda: path.open("rb"), label=f"green contract {path}") as stream:
        return stream.read()


def read_text(path):
    return read_bytes(path).decode("utf-8")
