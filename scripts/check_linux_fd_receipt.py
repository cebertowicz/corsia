#!/usr/bin/env python3
"""Require the exact successful cases in the Linux inherited-FD JUnit receipt."""
from collections import Counter
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

CLASS = "tests.test_inherited_fd_envelope"
EXPECTED = {"test_probe_programs_parse"} | {
    f"test_actual_envelope_transport_and_sibling[{scenario}-{mode}]"
    for scenario in ("positive", "extra", "missing")
    for mode in ("foreground", "background")
}


def check(path: Path) -> None:
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError("invalid JUnit root")
    suites = list(root.iter("testsuite"))
    if not suites or sum(int(suite.get("tests", "0")) for suite in suites) != 7:
        raise ValueError("invalid suite test count")
    cases = root.findall(".//testcase")
    names = Counter((case.get("classname"), case.get("name")) for case in cases)
    expected = Counter({(CLASS, name): 1 for name in EXPECTED})
    if names != expected:
        raise ValueError(f"unexpected test cases: {names}; expected: {expected}")
    for case in cases:
        if any(case.find(kind) is not None for kind in ("skipped", "failure", "error")):
            raise ValueError(f"unsuccessful test: {case.get('name')}")
    # A collection error can appear on a suite without a normal testcase.
    for suite in root.iter("testsuite"):
        for key in ("skipped", "failures", "errors"):
            if int(suite.get(key, "0")) != 0:
                raise ValueError(f"suite reports {key}")
    print("Linux inherited-FD receipt: all seven expected cases passed")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("expected one JUnit receipt path")
        check(Path(sys.argv[1]))
    except (OSError, ET.ParseError, ValueError) as error:
        print(f"Invalid Linux inherited-FD receipt: {error}", file=sys.stderr)
        sys.exit(1)
