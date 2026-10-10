"""Process prerequisites that must succeed before database startup."""

from __future__ import annotations

import os


def prepare_startup_prerequisites() -> None:
    """Settle this process's host ceilings, then prepare production assets."""
    from infra.config import MAX_CONCURRENT_IMAGE_NORMALIZATIONS
    from infra.file_descriptors import raise_file_descriptor_limit
    from infra.frontend_build import ensure_frontend_built
    from infra.host_resources import log_image_decode_budget

    raise_file_descriptor_limit()
    log_image_decode_budget(MAX_CONCURRENT_IMAGE_NORMALIZATIONS)
    # The installed plist pins this generation and rejects .env overrides.
    # Test mode is valid only for isolated direct-launch test processes.
    is_installed_service = "CORSIA_SERVICE_GENERATION" in os.environ
    if is_installed_service or os.environ.get("CORSIA_TEST_MODE") != "1":
        ensure_frontend_built()
