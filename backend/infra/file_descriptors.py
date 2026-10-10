"""Raise this process's own file-descriptor ceiling at startup.

macOS ships a 256 descriptor soft limit, which an orchestrator exhausts fast:
every harness app-server, websocket, subprocess pipe and pooled database
connection costs one. Exhaustion does not fail cleanly — it surfaces as
`OSError: [Errno 24]` from whichever unrelated component happened to open a
file next, so checkpoint generation, static asset serving and delivery
resolution all fail at once for a reason none of them own.

The soft limit is raised here rather than in the launcher because a process may
raise its own soft limit up to the hard limit without privilege. That keeps the
fix inside the application, where it is deployed and reviewed with everything
else, instead of in host configuration that has to be applied by hand.
"""

from __future__ import annotations

import resource

from loguru import logger

# Comfortably above real concurrent usage and far below macOS's
# ``kern.maxfilesperproc`` (245,760 on the current host), so the request
# succeeds rather than being silently clamped.
DESIRED_FILE_DESCRIPTOR_LIMIT = 16_384


def raise_file_descriptor_limit(
    desired: int = DESIRED_FILE_DESCRIPTOR_LIMIT,
) -> int:
    """Raise the soft descriptor limit toward ``desired``; return what we hold.

    Never lowers an already-generous limit, and never raises past the hard
    limit. A refusal is logged and the current limit returned: the process
    still runs, but the reason it may exhaust descriptors is on the record
    instead of being rediscovered from an unrelated component's failure.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= desired:
        return soft

    target = desired if hard == resource.RLIM_INFINITY else min(desired, hard)
    if target <= soft:
        logger.warning(
            "file descriptor soft limit is {} and the hard limit {} does not "
            "allow raising it; descriptor exhaustion is likely under load",
            soft,
            hard,
        )
        return soft

    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as exc:
        logger.warning(
            "could not raise the file descriptor soft limit from {} to {}: {}",
            soft,
            target,
            exc,
        )
        return soft

    logger.info("file descriptor soft limit raised from {} to {}", soft, target)
    return target
