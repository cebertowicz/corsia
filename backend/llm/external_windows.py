"""The context window an External model row is cataloged with."""

from __future__ import annotations

# litellm's model table keys each provider's models under its own namespace.
# The endpoint's host is the only evidence of which provider serves a model --
# the dialect is just the wire format -- so a window is looked up only where the
# host names the provider, and only by exact model id. Anything else stays
# unknown rather than borrowing another provider's number.
_REFERENCE_NAMESPACES: dict[str, tuple[str, ...]] = {
    "aliyuncs.com": ("dashscope",),
}

# Keys a ``/v1/models`` entry uses for the window the server serves (vLLM's
# ``max_model_len``, llama.cpp's ``n_ctx``, gateways' ``context_length``).
# Training lengths such as ``max_position_embeddings`` or ``n_ctx_train`` are
# not what the server accepts, so they never count.
_SERVING_WINDOW_KEYS = (
    "max_context_window", "context_length", "context_window", "max_model_len", "n_ctx",
)


def reported_window(entry: dict) -> int | None:
    """The serving window a ``/v1/models`` entry reports at its top level, if any."""
    from llm.model_metadata import extract_context_length

    return extract_context_length({key: entry.get(key) for key in _SERVING_WINDOW_KEYS})


def discovered_window(
    user_window: int | None, reported: int | None, host: str, model_id: str,
) -> int | None:
    """The user's per-server window wins; then the one the server reports for the
    model in ``/v1/models``; otherwise the provider's published one."""
    return user_window or reported or reference_context_window(host, model_id)


def reference_context_window(host: str, model_id: str) -> int | None:
    """The provider-published input window for ``model_id`` on ``host``, if known."""
    host = (host or "").lower().rstrip(".")
    namespaces = next(
        (ns for suffix, ns in _REFERENCE_NAMESPACES.items()
         if host == suffix or host.endswith("." + suffix)),
        (),
    )
    if not namespaces:
        return None
    import litellm

    for namespace in namespaces:
        window = (litellm.model_cost.get(f"{namespace}/{model_id}") or {}).get("max_input_tokens")
        if isinstance(window, int) and window > 0:
            return window
    return None
