"""Manifest SHA-256 pins for accepted local corrections-classifier assets.

The asset build script updates this mapping as part of the same reviewed change that
introduces an asset version. An unpinned local staging asset may be used only in
disposable test mode.
"""

PINNED_MANIFESTS: dict[str, str] = {
    "v1": "c298cae0e325015143be7de0749c00d4fcce7521868737f38e235f9734eb11d6",
}
