"""Manifest SHA-256 pins for accepted local corrections-classifier assets.

The asset build script updates this mapping as part of the same reviewed change that
introduces an asset version. An unpinned local staging asset may be used only in
disposable test mode.
"""

PINNED_MANIFESTS: dict[str, str] = {
    "v1": "9e24b1d053a237c2bb3adf3ce879e90f803569b4477446fa143136918d89e220",
}
