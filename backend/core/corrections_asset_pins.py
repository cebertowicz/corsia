"""Manifest SHA-256 pins for accepted local corrections-classifier assets.

The asset build script updates this mapping as part of the same reviewed change that
introduces an asset version. An unpinned local staging asset may be used only in
disposable test mode.
"""

PINNED_MANIFESTS: dict[str, str] = {
    "v1": "fa36f7194cef9682026bd097eaa979f847fdd80e6dc715c153ca67342fb290bc",
}
