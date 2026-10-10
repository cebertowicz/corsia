"""Trusted artifact presentation registry — descriptor resolution by
(integration, type), core handlers, and the honest unsupported fallback."""

from __future__ import annotations

from registry import presentation


def test_known_core_types_resolve_expected_capability():
    cases = {
        ("workspace", "document"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("workspace", "code"): presentation.CAPABILITY_LEAF_PACKAGE,
        ("workspace", "skill"): presentation.CAPABILITY_LEAF_PACKAGE,
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
