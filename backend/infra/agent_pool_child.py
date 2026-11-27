"""Private subprocess entry point for the fixture-only agent pool."""
from __future__ import annotations

import json
import logging
import os
import struct
import sys
import time

MAX_FRAME = 1024 * 1024


def lower_priority() -> str:
    try:
        if os.name == "nt":
            import ctypes
            kernel = getattr(ctypes, "windll").kernel32
            if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000):
                raise OSError("SetPriorityClass failed")
        else:
            os.nice(10)
        return "lowered"
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not lower worker priority (errno=%s): %s", exc.errno, exc)
        return "permission_denied" if isinstance(exc, PermissionError) else "failed"
    except Exception as exc:
        logging.getLogger(__name__).warning("Could not lower worker priority: %s", exc)
        return "failed"


def read_frame(stream):
    header = stream.read(4)
    if len(header) != 4:
        raise EOFError("incomplete frame header")
    length = struct.unpack("!I", header)[0]
    if length == 0 or length > MAX_FRAME:
        raise ValueError("invalid frame size")
    data = stream.read(length)
    if len(data) != length:
        raise EOFError("incomplete frame")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("frame must be an object")
    return value


def write_frame(stream, value):
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("frame too large")
    stream.write(struct.pack("!I", len(data)) + data)
    stream.flush()


def main() -> None:
    priority = lower_priority()
    # Keep work imports below the priority change. The only operation is a pure fixture.
    write_frame(sys.stdout.buffer, {"version": 1, "type": "ready", "priority": priority})
    while True:
        try:
            message = read_frame(sys.stdin.buffer)
            if message.get("version") == 1 and message.get("type") == "cancel":
                return
            if message.get("version") != 1 or message.get("type") != "call":
                raise ValueError("invalid envelope")
            identity = message.get("identity")
            if not isinstance(identity, dict) or not all(isinstance(identity.get(k), str) and identity[k] for k in ("call_id", "agent_id", "thread_id")):
                raise ValueError("invalid identity")
            if message.get("operation") != "fixture.echo":
                raise ValueError("unknown operation")
            payload = message.get("payload")
            if os.environ.get("CORSIA_TEST_MODE") == "1" and isinstance(payload, dict):
                if payload.get("_test_crash"):
                    os._exit(7)
                if payload.get("_test_delay"):
                    time.sleep(float(payload["_test_delay"]))
            write_frame(sys.stdout.buffer, {"version": 1, "type": "result", "identity": identity, "payload": payload})
            # Wait for the next frame; the parent owns idle expiry.
        except (EOFError, OSError, ValueError, TypeError, json.JSONDecodeError):
            return


if __name__ == "__main__":
    main()
