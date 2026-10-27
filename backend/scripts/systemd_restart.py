"""Start the sanitized restart owner from a private inherited pipe."""

import json
import os
import sys


def main() -> None:
    payload = json.load(sys.stdin)
    command = payload["command"]
    environment = payload["environment"]
    os.execve(command[0], command, environment)


if __name__ == "__main__":
    main()
