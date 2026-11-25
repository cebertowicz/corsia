"""Input and source-claim rules for durable publication."""

from pathlib import Path


def validate_publication_input(source: Path | None, content: bytes | None) -> None:
    if (source is None) == (content is None):
        raise ValueError("A durable publication requires bytes or one promotion source.")


def source_claim(source: Path | None, intent_id: str) -> Path | None:
    if source is None:
        return None
    return source.parent / f".corsia-write-{intent_id}.source"
