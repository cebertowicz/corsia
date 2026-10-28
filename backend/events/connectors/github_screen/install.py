"""Reconcile the shipped GitHub screen with a project's conductor and repositories."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from automation.script_schedules import DEFAULT_TIMEOUT_SECONDS, UNBOUNDED_RUNS, file_sha256
from automation.worker_inputs import owner_root
from events.connectors import get_manifest
from events.connectors.github_notifications_feed import github_repositories_for_policy
from infra.database import write_transaction
from infra.measure_registry import REGISTRY, persist_declaration
from infra.project_repo_policy import list_registered_repo_policies
from infra.write_effects import after_commit

ORIGIN_PREFIX = "rule_set:github@"
WORKFLOW = "github-pr-screen"
PACKAGE = Path(__file__).parent


def origin() -> str:
    return ORIGIN_PREFIX + get_manifest("github").version


def _key(project: str) -> str:
    return "github_screen_" + hashlib.sha256(project.encode()).hexdigest()[:20]


def _namespace(project: str) -> str:
    return "screen_" + hashlib.sha256(project.encode()).hexdigest()[:20]


def _write_if_changed(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def _repos(db, project: str) -> list[str]:
    policies = list_registered_repo_policies(project_id=project, db=db)
    return sorted({repo for policy in policies for repo in github_repositories_for_policy(policy)}, key=str.casefold)


def _declaration(project: str) -> tuple[str, str]:
    namespace = _namespace(project)
    spec = {"attributes": {"repo": "id", "pr_number": "count", "item_version": "id",
                           "request_identity": "id", "staging_path": "id",
                           "source": {"enum": ["outside"]}},
            "dedupe": ["request_identity"], "use": "route"}
    return namespace, json.dumps(spec, sort_keys=True, separators=(",", ":"))


def _retire_declaration(db, project: str, owner: str, path: str) -> None:
    persist_declaration(db, owner, path, _namespace(project), [], "")


def _schedule(db, project: str, owner: str, script: Path, now: str) -> str:
    schedule_id = _key(project) + "_script"
    digest = file_sha256(script)
    payload = json.dumps({"series": [], "workflow": None}, sort_keys=True)
    db.execute(
        "INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,"
        "payload_json,created_by_actor,created_at,updated_at,script_path,script_sha256,timeout_seconds) "
        "VALUES (?,?,?,'script','',NULL,?,'active',?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET thread_id=excluded.thread_id, scope_id=excluded.scope_id, "
        "kind='script', spec='', next_run_at=NULL, max_runs=excluded.max_runs, "
        "state='active', payload_json=excluded.payload_json, created_by_actor=excluded.created_by_actor, "
        "script_path=excluded.script_path, script_sha256=excluded.script_sha256, "
        "timeout_seconds=excluded.timeout_seconds, updated_at=excluded.updated_at "
        "WHERE thread_id IS NOT excluded.thread_id OR scope_id IS NOT excluded.scope_id "
        "OR kind IS NOT 'script' OR spec IS NOT '' OR next_run_at IS NOT NULL "
        "OR max_runs IS NOT excluded.max_runs OR state IS NOT 'active' OR payload_json IS NOT excluded.payload_json "
        "OR created_by_actor IS NOT excluded.created_by_actor OR script_path IS NOT excluded.script_path "
        "OR script_sha256 IS NOT excluded.script_sha256 OR timeout_seconds IS NOT excluded.timeout_seconds",
        (schedule_id, owner, project, UNBOUNDED_RUNS, payload, owner, now, now, str(script), digest,
         DEFAULT_TIMEOUT_SECONDS),
    )
    return schedule_id


def _rule(db, *, rule_id: str, project: str, owner: str, connector: str, event_type: str,
          tests: list[dict], action: str, script_id: str | None, now: str) -> None:
    values = (rule_id, now, now, "project:" + project, connector, event_type,
              json.dumps(tests, separators=(",", ":")), action, "thread:" + owner, origin(), owner,
              script_id, WORKFLOW if action == "start_worker" else None,
              "request_identity" if action == "start_worker" else None)
    db.execute(
        'INSERT INTO event_rules (id,created_at,updated_at,enabled,kind,scope,connector,event_type,"match",'
        'action,"to",origin,created_by,authority_ref,script_id,workflow_id,request_identity_fact) '
        "VALUES (?,?,?,1,'route',?,?,?,?,?,?,?,?,'self',?,?,?) "
        'ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at, enabled=1, scope=excluded.scope, '
        'connector=excluded.connector,event_type=excluded.event_type,"match"=excluded."match",'
        'action=excluded.action,"to"=excluded."to",origin=excluded.origin,created_by=excluded.created_by,'
        'authority_ref=excluded.authority_ref,script_id=excluded.script_id,workflow_id=excluded.workflow_id,'
        'request_identity_fact=excluded.request_identity_fact '
        'WHERE event_rules.origin LIKE ? AND (origin IS NOT excluded.origin OR enabled IS NOT 1 OR scope IS NOT excluded.scope '
        'OR connector IS NOT excluded.connector OR event_type IS NOT excluded.event_type '
        'OR "match" IS NOT excluded."match" OR action IS NOT excluded.action OR "to" IS NOT excluded."to" '
        'OR created_by IS NOT excluded.created_by OR authority_ref IS NOT excluded.authority_ref '
        'OR script_id IS NOT excluded.script_id '
        'OR workflow_id IS NOT excluded.workflow_id OR request_identity_fact IS NOT excluded.request_identity_fact)',
        (*values, ORIGIN_PREFIX + "%"),
    )


def sync(db, project: str | None = None) -> None:
    """Reconcile all projects, or one after its designation or repository changed."""
    projects = ([project] if project else [row[0] for row in db.execute("SELECT id FROM projects")])
    for project_id in projects:
        row = db.execute("SELECT conductor_thread_id FROM projects WHERE id=?", (project_id,)).fetchone()
        owner = row[0] if row else None
        repos = _repos(db, project_id) if owner else []
        key = _key(project_id)
        old = db.execute("SELECT created_by FROM event_rules WHERE id=? AND origin LIKE ?", (key + "_start", ORIGIN_PREFIX + "%")).fetchone()
        previous = old[0] if old else None
        with write_transaction(db, scope="sync_github_screen") as conn:
            if previous and (previous != owner or not repos):
                root = owner_root(previous, project_id, conn)
                _retire_declaration(conn, project_id, previous, str(root / "github-screen" / key / "measures.yaml"))
                after_commit(conn, lambda: REGISTRY.rebuild(db))
            if not owner or not repos:
                conn.execute("DELETE FROM event_rules WHERE id IN (?,?,?) AND origin LIKE ?",
                             (key + "_opened", key + "_synchronize", key + "_start", ORIGIN_PREFIX + "%"))
                conn.execute("DELETE FROM automation_schedules WHERE id=?", (key + "_script",))
                continue
            root = owner_root(owner, project_id, conn)
            folder = root / "github-screen" / key
            for name in ("stage.py", "floor.py"):
                _write_if_changed(folder / name, (PACKAGE / name).read_bytes())
            namespace, spec = _declaration(project_id)
            config = {"project_id": project_id, "repositories": repos, "staging_root": str(root / "staging"),
                      "staged_type": namespace + ".pr_staged"}
            _write_if_changed(folder / "config.json", json.dumps(config, sort_keys=True).encode())
            path = str(folder / "measures.yaml")
            persist_declaration(conn, owner, path, namespace, [("event", namespace + ".pr_staged", spec)], "shipped")
            after_commit(conn, lambda: REGISTRY.rebuild(db))
            now = datetime.now(timezone.utc).isoformat()
            schedule = _schedule(conn, project_id, owner, folder / "stage.py", now)
            tests: list[dict] = [{"field": "repo", "op": "in", "value": repos},
                     {"field": "author_origin", "op": "eq", "value": "outside"}]
            for suffix in ("opened", "synchronize"):
                _rule(conn, rule_id=key + "_" + suffix, project=project_id, owner=owner,
                      connector="github", event_type="pr." + suffix, tests=tests,
                      action="run_script", script_id=schedule, now=now)
            _rule(conn, rule_id=key + "_start", project=project_id, owner=owner,
                  connector=namespace, event_type="pr_staged", tests=[tests[0]], action="start_worker",
                  script_id=None, now=now)
