from __future__ import annotations

import json

import pytest


class TestFindingParser:
    def test_valid_json_array(self):
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([
            {
                "severity": "block",
                "file": "backend/core/turns.py",
                "line": 42,
                "category": "correctness/silent-error",
                "description": "Exception swallowed without logging",
                "cited_section": None,
            },
            {
                "severity": "nit",
                "file": "backend/llm/router.py",
                "line": 10,
                "category": "style/readability",
                "description": "Variable name unclear",
                "cited_section": None,
            },
        ])
        findings = parse_findings(raw)
        assert len(findings) == 2
        assert findings[0]["severity"] == "block"
        assert findings[0]["file"] == "backend/core/turns.py"
        assert findings[1]["severity"] == "nit"

    def test_empty_output_raises(self):
        from reviewer.reviewer_parser import ReviewerOutputError, parse_findings
        with pytest.raises(ReviewerOutputError):
            parse_findings("")
        with pytest.raises(ReviewerOutputError):
            parse_findings("   ")

    def test_empty_array(self):
        from reviewer.reviewer_parser import parse_findings
        assert parse_findings("[]") == []

    def test_malformed_json_raises(self):
        from reviewer.reviewer_parser import ReviewerOutputError, parse_findings
        with pytest.raises(ReviewerOutputError):
            parse_findings("not json at all")

    def test_billing_error_string_raises(self):
        from reviewer.reviewer_parser import ReviewerOutputError, parse_findings
        with pytest.raises(ReviewerOutputError):
            parse_findings("Credit balance is too low")

    def test_json_in_code_fences(self):
        from reviewer.reviewer_parser import parse_findings
        raw = '```json\n[{"severity":"block","file":"x.py","line":1,"category":"correctness/logic-error","description":"bug","cited_section":null}]\n```'
        findings = parse_findings(raw)
        assert len(findings) == 1
        assert findings[0]["severity"] == "block"

    def test_json_with_leading_text(self):
        from reviewer.reviewer_parser import parse_findings
        raw = 'Here are my findings:\n[{"severity":"nit","file":"a.py","line":1,"category":"style/dead-code","description":"unused","cited_section":null}]'
        findings = parse_findings(raw)
        assert len(findings) == 1

    def test_invalid_severity_defaults_to_nit(self):
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([{
            "severity": "critical",
            "file": "x.py",
            "line": 1,
            "category": "correctness/logic-error",
            "description": "test",
            "cited_section": None,
        }])
        findings = parse_findings(raw)
        assert findings[0]["severity"] == "nit"

    def test_unknown_category_kept(self):
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([{
            "severity": "flag",
            "file": "x.py",
            "line": 1,
            "category": "custom/made-up",
            "description": "test",
            "cited_section": None,
        }])
        findings = parse_findings(raw)
        assert findings[0]["category"] == "custom/made-up"

    def test_missing_fields_get_defaults(self):
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([{"description": "something wrong"}])
        findings = parse_findings(raw)
        assert len(findings) == 1
        assert findings[0]["severity"] == "nit"
        assert findings[0]["file"] == "unknown"
        assert findings[0]["line"] == 0

    def test_non_array_raises(self):
        from reviewer.reviewer_parser import ReviewerOutputError, parse_findings
        with pytest.raises(ReviewerOutputError):
            parse_findings('{"severity": "block"}')

    def test_line_as_string_is_converted(self):
        from reviewer.reviewer_parser import parse_findings
        raw = json.dumps([{
            "severity": "nit",
            "file": "x.py",
            "line": "42",
            "category": "style/readability",
            "description": "test",
            "cited_section": None,
        }])
        findings = parse_findings(raw)
        assert findings[0]["line"] == 42
