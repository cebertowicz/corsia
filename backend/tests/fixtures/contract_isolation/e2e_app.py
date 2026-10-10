"""Tiny ASGI process used only to exercise the matrix launcher's OS boundary."""

import json
import socket
import subprocess
import sys

from fastapi import FastAPI

app = FastAPI()


@app.get("/api/health")
def health():
    """Signal that the real child process has started."""
    return {"ok": True}


@app.post("/probe")
def probe(body: dict):
    """Connect from this process or an ordinary unspecialized descendant."""
    if body["child"]:
        program = """
import json, socket, sys
try:
    with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=2):
        result = 0
except OSError as error:
    result = error.errno
print(json.dumps({'errno': result}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", program, str(body["port"])],
            capture_output=True, text=True, check=True, timeout=5,
        )
        return json.loads(completed.stdout)
    try:
        with socket.create_connection(("127.0.0.1", body["port"]), timeout=2):
            return {"errno": 0}
    except OSError as error:
        return {"errno": error.errno}
