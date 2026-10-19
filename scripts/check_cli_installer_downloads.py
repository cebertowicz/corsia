#!/usr/bin/env python3
"""Verify that official CLI installer endpoints accept CORSIA's request builder."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from api.cli_install import INSTALL_URLS, _installer_request


def main() -> int:
    for vendor in INSTALL_URLS:
        with urlopen(_installer_request(vendor), timeout=30) as response:
            script = response.read(2)
            if response.status != 200 or script != b"#!":
                raise RuntimeError(f"{vendor}: expected HTTP 200 and a shell script")
        print(f"{vendor}: HTTP 200, shell script")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
