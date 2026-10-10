"""Mode-B helpers for YAML scenario execution."""

from __future__ import annotations

from typing import Any

_MODE_B_VALUES: frozenset[str] = frozenset({"b", "real_local", "real-local"})
_SPAN_LLM_CALL = "corsia.llm_call"
_SPAN_CONTEXT_BUILD = "corsia.context_build"


def is_mode_b(sample: Any) -> bool:
    metadata = dict(getattr(sample, "metadata", None) or {})
    mode_val = metadata.get("mode")
    if isinstance(mode_val, str) and mode_val.strip().lower() in _MODE_B_VALUES:
        return True

    pre = metadata.get("pre_conditions") or []
    has_mock = False
    if isinstance(pre, list):
        for pc in pre:
            if isinstance(pc, dict) and pc.get("fixture") == "mock_llm_replies":
                has_mock = True
                break

    decls = metadata.get("scorers") or []
    if isinstance(decls, list):
        names: set[str] = set()
        for raw in decls:
            if isinstance(raw, str):
                names.add(raw)
            elif isinstance(raw, dict) and raw.get("name"):
                names.add(str(raw["name"]))
        if names & {"hit_rate_per_turn", "system_prompt_stable", "ttft_under"}:
            return True

    if metadata.get("turns") and not has_mock:
        return True
    return False


def extract_turn_inputs(sample: Any) -> list[str]:
    metadata = dict(getattr(sample, "metadata", None) or {})

    raw_turns = metadata.get("turns")
    if isinstance(raw_turns, list) and raw_turns:
        prompts: list[str] = []
        for t in raw_turns:
            if not isinstance(t, dict):
                continue
            role = t.get("role", "user")
            if role and role != "user":
                continue
            content = _pick_content(t)
            if content:
                prompts.append(content)
        if prompts:
            return prompts

    raw_steps = metadata.get("steps")
    if isinstance(raw_steps, list) and raw_steps:
        prompts = []
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            if step.get("action") != "user_send":
                continue
            args = step.get("args") or {}
            content = _pick_content(args) if isinstance(args, dict) else ""
            if content:
                prompts.append(content)
        if prompts:
            return prompts

    return []


def resolve_metadata_model(metadata: dict[str, Any], sample: Any) -> str | None:
    explicit = metadata.get("model")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    mut = metadata.get("model_under_test")
    if isinstance(mut, str) and mut.strip():
        return mut.strip()
    return _extract_setup_model(sample)


def extract_span_attrs(response: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "trace_id": response.get("trace_id"),
        "ms_total": response.get("ms_total"),
        "iteration_count": response.get("iteration_count"),
    }

    usage = response.get("usage") or {}
    if isinstance(usage, dict):
        if usage.get("prompt_tokens"):
            out["prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
        if usage.get("completion_tokens"):
            out["completion_tokens"] = int(usage.get("completion_tokens") or 0)
        if usage.get("cache_read_tokens") is not None:
            out["cached_tokens_usage"] = int(usage.get("cache_read_tokens") or 0)

    span_summary = response.get("span_summary") or {}
    spans = span_summary.get("spans") or []
    if not isinstance(spans, list):
        return out

    last_llm: dict[str, Any] | None = None
    last_ctx: dict[str, Any] | None = None
    for sp in spans:
        if not isinstance(sp, dict):
            continue
        if sp.get("name") == _SPAN_LLM_CALL:
            last_llm = sp
        elif sp.get("name") == _SPAN_CONTEXT_BUILD:
            last_ctx = sp

    _copy_span_attrs(out, last_llm, "llm.")
    _copy_span_attrs(out, last_ctx, "context.")
    return out


def _pick_content(d: dict[str, Any]) -> str:
    content = d.get("content")
    if isinstance(content, str) and content.strip():
        return content
    text = d.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return ""


def _extract_setup_model(sample: Any) -> str | None:
    metadata = dict(getattr(sample, "metadata", None) or {})
    setup = metadata.get("setup")
    if isinstance(setup, dict):
        m = setup.get("model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    return None


def _copy_span_attrs(out: dict[str, Any], span: dict[str, Any] | None, prefix: str) -> None:
    if span is None:
        return
    attrs = span.get("attrs") or {}
    for k, v in attrs.items():
        if not isinstance(k, str):
            continue
        if k.startswith(prefix):
            out[k[len(prefix):]] = v
            out[k] = v
        else:
            out[k] = v
