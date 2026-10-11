"""Trusted artifact presentation registry — descriptor resolution by
(integration, type), core handlers, and the honest unsupported fallback."""

from __future__ import annotations

from registry import presentation


def test_known_core_types_resolve_expected_capability():
    cases = {
        ("workspace", "document"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("workspace", "code"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("workspace", "skill"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("workspace", "image"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("upload", "upload"): presentation.CAPABILITY_DOWNLOAD,
        ("github", "pull-request"): presentation.CAPABILITY_EXTERNAL_LINK,
    }
    for (integration, type_), capability in cases.items():
        descriptor = presentation.resolve_descriptor(integration, type_)
        assert descriptor.capability == capability
        assert descriptor.label
        assert descriptor.icon


def test_unknown_type_falls_back_to_honest_unsupported():
    descriptor = presentation.resolve_descriptor("some-connector", "mystery-widget")
    assert descriptor.capability == presentation.CAPABILITY_UNSUPPORTED
    assert descriptor.label == "Unsupported"


def test_never_raises_for_arbitrary_input():
    for integration, type_ in [("", ""), (None, None), ("x" * 50, "y" * 50)]:
        descriptor = presentation.resolve_descriptor(integration, type_)
        assert descriptor.capability in {
            presentation.CAPABILITY_LEAF_PACKAGE,
            presentation.CAPABILITY_EXTERNAL_LINK,
            presentation.CAPABILITY_DOWNLOAD,
            presentation.CAPABILITY_UNSUPPORTED,
        }


def test_register_descriptor_adds_a_connector_type_without_editing_control_flow():
    added = presentation.PresentationDescriptor(
        label="Widget", icon="widget", capability=presentation.CAPABILITY_LEAF_PACKAGE,
    )
    presentation.register_descriptor("test-connector", "widget", added)
    try:
        resolved = presentation.resolve_descriptor("test-connector", "widget")
        assert resolved == added
    finally:
        del presentation._DESCRIPTORS[("test-connector", "widget")]


def test_claude_code_session_and_codex_thread_are_non_pane_types():
    assert ("claude-code", "session") in presentation.NON_PANE_TYPES
    assert ("codex", "thread") in presentation.NON_PANE_TYPES


def test_workspace_image_descriptor_is_labelled_and_iconned():
    descriptor = presentation.resolve_descriptor("workspace", "image")
    assert (descriptor.label, descriptor.icon) == ("Image", "image")


def test_existing_local_image_upload_resolves_to_image_descriptor(tmp_path):
    image = tmp_path / "receipt.png"
    image.write_bytes(b"png")

    descriptor = presentation.resolve_descriptor(
        "upload", "upload", path=str(image), metadata={"mime": "image/png"}, title=image.name,
    )

    assert (descriptor.label, descriptor.icon, descriptor.capability) == (
        "Image", "image", presentation.CAPABILITY_LEAF_PACKAGE,
    )


def test_existing_local_image_upload_uses_filename_when_mime_is_missing(tmp_path):
    image = tmp_path / "photo.JPEG"
    image.write_bytes(b"jpeg")

    descriptor = presentation.resolve_descriptor(
        "upload", "upload", path=str(image), metadata={}, title=image.name,
    )

    assert descriptor.capability == presentation.CAPABILITY_LEAF_PACKAGE


def test_existing_local_pdf_upload_stays_downloadable(tmp_path):
    pdf = tmp_path / "receipt.pdf"
    pdf.write_bytes(b"%PDF")

    descriptor = presentation.resolve_descriptor(
        "upload", "upload", path=str(pdf), metadata={"mime": "application/pdf"}, title=pdf.name,
    )

    assert descriptor.capability == presentation.CAPABILITY_DOWNLOAD


def test_missing_image_upload_stays_downloadable(tmp_path):
    missing = tmp_path / "missing.webp"

    descriptor = presentation.resolve_descriptor(
        "upload", "upload", path=str(missing), metadata={"mime": "image/webp"}, title=missing.name,
    )

    assert descriptor.capability == presentation.CAPABILITY_DOWNLOAD
