"""Controls for the exact Linux inherited-descriptor JUnit receipt."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_linux_fd_receipt import CLASS, EXPECTED, check


def receipt(path, *, names=EXPECTED, outcome=None, classname=CLASS):
    suite = ET.Element("testsuite", tests=str(len(names)), skipped="0", failures="0", errors="0")
    for name in names:
        case = ET.SubElement(suite, "testcase", classname=classname, name=name)
        if outcome:
            ET.SubElement(case, outcome)
            outcome = None
    ET.ElementTree(suite).write(path)


def test_exact_success(tmp_path):
    path = tmp_path / "receipt.xml"
    receipt(path)
    check(path)


@pytest.mark.parametrize("change", ["missing", "duplicate", "unrelated", "skipped", "failure", "error", "wrong_class", "suite_error"])
def test_rejects_invalid_receipts(tmp_path, change):
    path = tmp_path / "receipt.xml"
    names = sorted(EXPECTED)
    if change == "missing":
        names.pop()
    elif change == "duplicate":
        names[-1] = names[0]
    elif change == "unrelated":
        names = [f"unrelated_{n}" for n in range(7)]
    receipt(path, names=names, outcome=change if change in {"skipped", "failure", "error"} else None,
            classname="other" if change == "wrong_class" else CLASS)
    if change == "suite_error":
        tree = ET.parse(path)
        tree.getroot().set("errors", "1")
        tree.write(path)
    with pytest.raises(ValueError):
        check(path)
