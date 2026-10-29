"""Safe diagnostic reasons for failed cache-warm requests."""

from loguru import logger


def log_warm_failure(stage: str, result: dict | None = None) -> None:
    """Log a bounded server-selected reason without request or provider text."""
    message = (result or {}).get("message", "")
    if isinstance(message, str) and message.startswith("model not loaded:"):
        reason = "model not loaded"
    elif message == "Warm was cancelled":
        reason = "warm was cancelled"
    elif message == "Not a warmable available Native oMLX network model":
        reason = "model is not warmable"
    elif isinstance(message, str) and message.startswith("Exact context-window evidence is unknown for "):
        reason = "context-window evidence unknown"
    elif (result or {}).get("skipped"):
        reason = "active delivery work"
    elif stage == "target":
        reason = "target resolution failed"
    elif stage == "proof":
        reason = "target validation failed"
    elif stage == "prefix":
        reason = "prefix construction failed"
    else:
        reason = "provider warm failed"
    logger.info("warm_session_cache skipped: warm_failed: {}", reason)
