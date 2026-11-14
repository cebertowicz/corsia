"""Human-only tool-detail projection; durable provider and agent history is unchanged."""
import json


def is_post_update(call):
    """Recognize native and bridged post_update calls by their declared tool identity."""
    name = call.get("name", "")
    if name.removeprefix("mcp__corsia__") == "post_update":
        return True
    if name.removeprefix("mcp__corsia__") not in ("corsia_tool", "tool_call"):
        return False
    args = call.get("args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return False
    return isinstance(args, dict) and args.get("name") == "post_update"


def hide_update_details(display):
    """Remove duplicate post-update argument/result text from every stored human page."""
    for row in display:
        meta = row.get("metadata")
        if not isinstance(meta, dict):
            continue
        for call in meta.get("tool_calls") or []:
            if isinstance(call, dict) and is_post_update(call):
                call["args"] = ""
                call["result"] = ""
                call["result_state"] = "loaded"
                call.pop("error_line", None)
