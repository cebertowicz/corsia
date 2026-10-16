"""The folder an imported app was set up for, kept as a display label and nothing else (Governance C3).

Only the folder's last path segment is ever stored or returned. No code reads the label for reach,
working directory, launch, sandbox profile, args or matching; a test lists every reader.
"""
from __future__ import annotations

import unicodedata

MAX_FOLDER_LABEL = 60
SOURCE_FOLDER_KEY = "source_folder"


def clean_folder_label(value: object) -> str | None:
    """Return plain text for display: control and separator characters removed, capped with an ellipsis."""
    if not isinstance(value, str):
        return None
    text = "".join(
        " " if unicodedata.category(char) in {"Zs", "Zl", "Zp"} else char
        for char in value
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Co", "Cn"} or char == " "
    )
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= MAX_FOLDER_LABEL else text[: MAX_FOLDER_LABEL - 1].rstrip() + "…"
