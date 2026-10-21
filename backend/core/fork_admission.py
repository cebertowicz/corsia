"""Provider-only admission shared by all disposable fork kinds."""

from core.checkpoint_render import require_cold_fork_room
from core.fork_runtime import ForkKind
from core.fork_types import ColdForkSource, ForkNotAdmitted, ProviderForkRequest


def build_cold_fork_admission(*, fork_id: str, thread_id: str, target,
                               kind: ForkKind, instruction: str, source: ColdForkSource,
                               working_dir: str, reasoning_effort: str | None,
                               binary: str | None, mcp_config: str | None = None) -> ProviderForkRequest:
    """Freeze a kind's instruction and source without creating checkpoint identity."""
    if target.harness not in kind.step_limits:
        raise ForkNotAdmitted(f"{kind.name} cannot enforce its policy on {target.harness}")
    require_cold_fork_room(
        target.context_window, instruction, writer=target, protected=source.protected_blocks,
    )
    return ProviderForkRequest(
        fork_id=fork_id, thread_id=thread_id, target=target,
        instruction=instruction, cold_source=source, reasoning_effort=reasoning_effort,
        working_dir=working_dir, binary=binary, mcp_config=mcp_config,
    )
