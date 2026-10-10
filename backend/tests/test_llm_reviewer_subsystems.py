from __future__ import annotations


class TestReviewerPathHelpers:
    def test_extract_changed_files_from_diff(self):
        from reviewer.reviewer_subsystems import extract_changed_files_from_diff
        diff = (
            "diff --git a/backend/core/turns.py b/backend/core/turns.py\n"
            "--- a/backend/core/turns.py\n"
            "+++ b/backend/core/turns.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+import os\n"
            " def foo():\n"
            "     pass\n"
            "diff --git a/backend/llm/router.py b/backend/llm/router.py\n"
            "--- a/backend/llm/router.py\n"
            "+++ b/backend/llm/router.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        files = extract_changed_files_from_diff(diff)
        assert "backend/core/turns.py" in files
        assert "backend/llm/router.py" in files

class TestCategories:
    def test_valid_categories_nonempty(self):
        from reviewer.reviewer_categories import VALID_CATEGORIES
        assert len(VALID_CATEGORIES) > 10

    def test_severity_set(self):
        from reviewer.reviewer_categories import VALID_SEVERITIES
        assert VALID_SEVERITIES == {"block", "flag", "nit"}

    def test_retry_constants(self):
        from reviewer.reviewer_categories import MAX_RETRIES, RETRY_BACKOFF_MINUTES
        assert RETRY_BACKOFF_MINUTES == (1, 2, 4, 8, 16)
        assert MAX_RETRIES == 5
