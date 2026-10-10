from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading

import pytest


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    import infra.database as db_mod

    db_path = tmp_path / "test.db"
    monkeypatch.setattr("infra.config.DB_PATH", db_path)
    monkeypatch.setattr("infra.database.DB_PATH", db_path)

    db_mod.close_thread_db()
    db_mod._thread_local = threading.local()
    db_mod._all_connections.clear()

    from infra.schema import SCHEMA_SQL
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()

    yield db_path

    db_mod.close_thread_db()
    db_mod._thread_local = threading.local()
    db_mod._all_connections.clear()


WEBHOOK_SECRET = "test-secret-s6-0a"


def _make_signature(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _pr_opened_payload(repo: str = "owner/repo", pr_number: int = 42) -> dict:
    return {
        "action": "opened",
        "pull_request": {
            "number": pr_number,
            "title": "Test PR",
            "merged": False,
        },
        "repository": {"full_name": repo},
    }


def _pr_ready_for_review_payload(
    repo: str = "owner/repo", pr_number: int = 42,
    head_sha: str = "abc123def",
) -> dict:
    return {
        "action": "ready_for_review",
        "pull_request": {
            "number": pr_number,
            "draft": False,
            "head": {"sha": head_sha},
            "base": {"ref": "dev"},
        },
        "repository": {"full_name": repo},
    }


def _issue_comment_on_pr_payload() -> dict:
    return {
        "action": "created",
        "issue": {
            "number": 42,
            "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/42"},
        },
        "comment": {"body": "Fix this please"},
        "repository": {"full_name": "owner/repo"},
    }


def _check_suite_payload(
    repo: str = "owner/repo",
    pr_number: int = 42,
    head_sha: str = "abc123def",
    conclusion: str = "success",
) -> dict:
    return {
        "action": "completed",
        "check_suite": {
            "conclusion": conclusion,
            "head_sha": head_sha,
            "pull_requests": [
                {
                    "number": pr_number,
                    "base": {"ref": "dev"},
                    "head": {"sha": head_sha},
                },
            ],
        },
        "repository": {"full_name": repo},
    }


def _issue_comment_on_issue_payload() -> dict:
    return {
        "action": "created",
        "issue": {"number": 10},
        "comment": {"body": "This is a plain issue comment"},
        "repository": {"full_name": "owner/repo"},
    }
