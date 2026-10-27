"""Start the sanitized restart owner from a private inherited pipe."""

import json
import os
import sys


def main() -> None:
    payload = json.loads(sys.stdin.readline())
    command = payload["command"]
    environment = payload["environment"]
    print("ready", flush=True)
    if sys.stdin.readline() != "go\n":
        return
    # stderr is the restart log supplied by the backend; stdout was only used
    # to acknowledge that the separately managed owner actually started.
    os.dup2(2, 1)
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
