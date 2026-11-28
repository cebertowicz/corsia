"""Bounded diagnostics from a pool child, drained for its whole lifetime."""
import logging


async def capture_stderr(worker) -> None:
    # Keep one bounded line while continuing to drain, even if the child floods stderr.
    pending = bytearray()
    while chunk := await worker.proc.stderr.read(4096):
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            pending.extend(part)
            del pending[:-1024]
            worker.diagnostic = pending.rstrip(b"\r").decode("utf-8", errors="replace")
            if worker.diagnostic.startswith("Could not lower worker priority"):
                logging.getLogger("infra.agent_pool").warning("%s", worker.diagnostic)
            if worker.diagnostic.startswith("worker error: "):
                worker.error_detail = worker.diagnostic
                worker.error_reported.set()
            pending.clear()
        pending.extend(parts[-1])
        del pending[:-1024]
