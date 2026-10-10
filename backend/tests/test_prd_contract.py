"""Stage-2b: load an APPROVED PRD's contract (§0.0 + Reviewer Contract).

Only a PRD whose ``§0.0 Approved`` checkbox is ticked is enforceable; a draft
neither injects nor blocks. The whole PRD is never injected — only the approved
§0.0 requirements plus the PRD Reviewer Contract if present.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_APPROVED_PRD = """\
# Connections

## The flow
- prose that must NOT be injected (not part of the contract).

## §0.0 Requirements (bullets-first gate)

- [x] §0.0 Approved (maintainer ticks per sub-area or whole)

### 0.0.1 - Connection identity
- **CX1.** A connection has a stable identity.

### 0.0.2 - Auth
- **CX2.** Credentials are user-owned.

## §I Reviewer Contract
- **Block:** a connection without identity (CX1).

## Appendix
- trailing prose that must NOT be injected.
"""

_DRAFT_PRD = _APPROVED_PRD.replace("- [x] §0.0 Approved", "- [ ] §0.0 Approved")

_APPROVED_NO_CONTRACT = """\
# Scopes

## §0.0 Requirements (bullets-first gate)

- [x] §0.0 Approved (maintainer ticks per sub-area or whole)

### 0.0.1 - Scope identity
- **SC1.** A scope has identity.
"""


def _write(tmp_path: Path, slug_filename: str, content: str) -> Path:
    corsia = tmp_path / "projects" / "corsia" / "shared" / "planning"
    corsia.mkdir(parents=True, exist_ok=True)
    (corsia / slug_filename).write_text(content, encoding="utf-8")
    return tmp_path


class TestLoadApprovedContract:
    def test_approved_prd_injects_section_0_0_and_reviewer_contract(self, tmp_path):
        from reviewer.prd_contract import load_approved_contract

        ws = _write(tmp_path, "PRD-CONNECTIONS.md", _APPROVED_PRD)
        contract = load_approved_contract("connections", ws)

        assert contract is not None
        assert "CX1" in contract and "CX2" in contract  # §0.0 requirements
        assert "Reviewer Contract" in contract  # §I present
        assert "must NOT be injected" not in contract  # non-contract prose excluded

    def test_uppercase_checkbox_is_approved(self, tmp_path):
        """`[X]` (valid Markdown) must count as approved, not silently draft."""
        from reviewer.prd_contract import load_approved_contract

        ws = _write(
            tmp_path, "PRD-CONNECTIONS.md",
            _APPROVED_PRD.replace("- [x] §0.0 Approved", "- [X] §0.0 Approved"),
        )
        assert load_approved_contract("connections", ws) is not None

    def test_draft_prd_is_not_enforceable(self, tmp_path):
        from reviewer.prd_contract import load_approved_contract

        ws = _write(tmp_path, "PRD-CONNECTIONS.md", _DRAFT_PRD)
        assert load_approved_contract("connections", ws) is None

    def test_missing_prd_file_returns_none(self, tmp_path):
        from reviewer.prd_contract import load_approved_contract

        ws = _write(tmp_path, "PRD-OTHER.md", _APPROVED_PRD)
        assert load_approved_contract("connections", ws) is None

    def test_approved_without_reviewer_contract_injects_section_0_0_only(self, tmp_path):
        from reviewer.prd_contract import load_approved_contract

        ws = _write(tmp_path, "PRD-SCOPES.md", _APPROVED_NO_CONTRACT)
        contract = load_approved_contract("scopes", ws)

        assert contract is not None
        assert "SC1" in contract
        assert "Reviewer Contract" not in contract  # absent is fine, not overclaimed

    def test_slug_maps_to_prd_filename(self):
        from reviewer.prd_contract import prd_filename

        assert prd_filename("connections") == "PRD-CONNECTIONS.md"
        assert prd_filename("event-delivery") == "PRD-EVENT-DELIVERY.md"

    def test_section_anchor_ignores_other_0_0_headings(self, tmp_path):
        """A `## Changelog v10.0.3` before §0.0 must not be picked as §0.0."""
        from reviewer.prd_contract import load_approved_contract

        prd = (
            "# Connections\n\n## Changelog v10.0.3\n- DECOY content.\n\n"
            "## §0.0 Requirements\n- [x] §0.0 Approved\n\n"
            "### 0.0.1\n- **CX1.** real requirement.\n"
        )
        ws = _write(tmp_path, "PRD-CONNECTIONS.md", prd)
        contract = load_approved_contract("connections", ws)

        assert contract is not None
        assert "CX1" in contract
        assert "DECOY" not in contract  # the changelog heading was not picked

    def test_approved_but_unparseable_raises_contract_error(self, tmp_path):
        """An approved PRD with no extractable contract must FAIL LOUD, not None.

        Returning None would conflate 'approved contract failed to parse' with a
        coverage gap ('no PRD owns these paths'), silently dropping a contract
        the reviewer is meant to enforce. It must raise so the runner's error
        path surfaces it red/retryable.
        """
        from reviewer.prd_contract import PRDContractError, load_approved_contract

        # Approved checkbox present, but no §0.0 heading and no Reviewer Contract.
        prd = "# Broken\n\n- [x] §0.0 Approved\n\nsome prose, no sections.\n"
        ws = _write(tmp_path, "PRD-CONNECTIONS.md", prd)

        with pytest.raises(PRDContractError, match="connections"):
            load_approved_contract("connections", ws)

    def test_contract_error_is_distinct_from_missing_or_draft(self, tmp_path):
        """Missing/draft stay None (coverage gap); only unparseable-approved raises."""
        from reviewer.prd_contract import load_approved_contract

        # Missing file and draft are legitimate "no enforceable contract" → None.
        assert load_approved_contract(
            "connections", _write(tmp_path, "PRD-OTHER.md", _APPROVED_PRD)
        ) is None
        assert load_approved_contract(
            "connections", _write(tmp_path, "PRD-CONNECTIONS.md", _DRAFT_PRD)
        ) is None
