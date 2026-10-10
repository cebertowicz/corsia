"""``corsia artifact <verb>`` — CLI over the audited artifact-registry lifecycle verbs.

Thin adapter: each subcommand invokes the SAME Tool the agent harnesses use
(``tools.artifacts.*``) with an explicit actor session, so there is ONE
authoritative lifecycle path (PRD-ARTIFACTS-LIFECYCLE AR2/AR3a) — no parallel
logic and no raw-SQL surgery to release / rehome / de-own an artifact.

The actor/principal is an explicit ``--as-session`` (the thread the CLI acts
as); the verb resolves the acting user from that thread exactly as a harness
tool call would. Acting as an artifact's owning thread is the legitimate,
audited de-own path. An owner-default (act as the workspace owner without
naming a thread) is a deliberate follow-up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--metadata must be valid JSON: {exc}")
    if not isinstance(value, dict):
        raise SystemExit("--metadata must be a JSON object")
    return value


def _invoke(tool: Any, as_thread: str, **kwargs: Any) -> int:
    """Run one artifact Tool as ``as_thread`` and print its result.

    Reuses the harness path verbatim: the Tool returns a JSON string on success
    or an ``Error: ...`` string on failure. Exit non-zero on the latter so the
    CLI fails loud (never a silent partial).
    """
    payload = {k: v for k, v in kwargs.items() if v is not None}
    result = asyncio.run(tool.execute(_thread_id=as_thread, **payload))
    print(result)
    return 1 if isinstance(result, str) and result.startswith("Error") else 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    from tools.artifacts import ArtifactResolveTool

    return _invoke(
        ArtifactResolveTool(), args.as_thread,
        artifact_id=args.artifact_id, path=args.path,
        integration=args.integration, type=args.type, external_id=args.external_id,
    )


def _cmd_register(args: argparse.Namespace) -> int:
    from tools.artifacts import ArtifactRegisterTool

    return _invoke(
        ArtifactRegisterTool(), args.as_thread,
        integration=args.integration, type=args.type, external_id=args.external_id,
        path=args.path, parent_thread_id=args.parent_thread_id,
        title=args.title, description=args.description, retention=args.retention,
        metadata=_parse_metadata(args.metadata),
    )


def _cmd_update(args: argparse.Namespace) -> int:
    from tools.artifacts import ArtifactUpdateTool

    return _invoke(
        ArtifactUpdateTool(), args.as_thread,
        artifact_id=args.artifact_id, description=args.description,
        hidden=args.hidden, thread_id=args.thread_id,
    )


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from tools.artifacts import ArtifactReconcileTool

    return _invoke(
        ArtifactReconcileTool(), args.as_thread,
        thread_id=args.thread_id, mode=args.mode,
    )


def _cmd_relocate(args: argparse.Namespace) -> int:
    from tools.artifacts import ArtifactRelocateTool

    return _invoke(
        ArtifactRelocateTool(), args.as_thread,
        artifact_id=args.artifact_id, target_path=args.target_path,
    )


def _cmd_rehome(args: argparse.Namespace) -> int:
    from tools.artifact_ownership import ArtifactRehomeTool

    return _invoke(
        ArtifactRehomeTool(), args.as_thread,
        integration=args.integration, type=args.type, external_id=args.external_id,
        new_owner_thread_id=args.new_owner_thread_id, authority=args.authority,
        reason=args.reason, metadata=_parse_metadata(args.metadata),
    )


def _cmd_release(args: argparse.Namespace) -> int:
    from tools.artifact_ownership import ArtifactReleaseTool

    return _invoke(
        ArtifactReleaseTool(), args.as_thread,
        integration=args.integration, type=args.type, external_id=args.external_id,
        authority=args.authority, reason=args.reason,
        triage_thread_id=args.triage_thread_id, metadata=_parse_metadata(args.metadata),
    )


def build_parser(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``artifact`` command group to the corsia CLI."""
    actor = argparse.ArgumentParser(add_help=False)
    actor.add_argument(
        "--as-session", required=True, dest="as_thread",
        help="Thread id the CLI acts as (the actor/principal for the audited verb).",
    )

    art = subparsers.add_parser(
        "artifact",
        help="Artifact registry lifecycle (resolve/register/update/reconcile/relocate/rehome/release).",
    )
    verbs = art.add_subparsers(dest="verb", required=True)

    resolve = verbs.add_parser("resolve", parents=[actor], help="Resolve an artifact by id, path, or integration/type/external_id.")
    resolve.add_argument("--artifact-id")
    resolve.add_argument("--path")
    resolve.add_argument("--integration")
    resolve.add_argument("--type")
    resolve.add_argument("--external-id")
    resolve.set_defaults(func=_cmd_resolve)

    register = verbs.add_parser("register", parents=[actor], help="Register an artifact to a thread.")
    register.add_argument("--integration")
    register.add_argument("--type")
    register.add_argument("--external-id")
    register.add_argument("--path")
    register.add_argument("--parent-thread-id")
    register.add_argument("--title")
    register.add_argument("--description")
    register.add_argument("--retention", choices=["ephemeral", "durable"], default=None)
    register.add_argument("--metadata", help="JSON object")
    register.set_defaults(func=_cmd_register)

    update = verbs.add_parser("update", parents=[actor], help="Update an artifact description or pane visibility.")
    update.add_argument("--artifact-id", required=True)
    update.add_argument("--description")
    hidden = update.add_mutually_exclusive_group()
    hidden.add_argument("--hide", dest="hidden", action="store_true", default=None)
    hidden.add_argument("--unhide", dest="hidden", action="store_false", default=None)
    update.add_argument("--thread-id")
    update.set_defaults(func=_cmd_update)

    reconcile = verbs.add_parser("reconcile", parents=[actor], help="Preview or apply known artifact pane repairs for a thread.")
    reconcile.add_argument("--thread-id")
    reconcile.add_argument("--mode", choices=["preview", "apply"], default=None)
    reconcile.set_defaults(func=_cmd_reconcile)

    relocate = verbs.add_parser("relocate", parents=[actor], help="Move an artifact's backing file, preserving identity.")
    relocate.add_argument("--artifact-id", required=True)
    relocate.add_argument("--target-path", required=True)
    relocate.set_defaults(func=_cmd_relocate)

    rehome = verbs.add_parser("rehome", parents=[actor], help="Transfer artifact ownership (audited).")
    rehome.add_argument("--integration", required=True)
    rehome.add_argument("--type", required=True)
    rehome.add_argument("--external-id", required=True)
    rehome.add_argument("--new-owner-thread-id", required=True)
    rehome.add_argument("--reason", required=True)
    rehome.add_argument("--authority")
    rehome.add_argument("--metadata", help="JSON object")
    rehome.set_defaults(func=_cmd_rehome)

    release = verbs.add_parser("release", parents=[actor], help="Release artifact ownership (audited de-own).")
    release.add_argument("--integration", required=True)
    release.add_argument("--type", required=True)
    release.add_argument("--external-id", required=True)
    release.add_argument("--reason", required=True)
    release.add_argument("--authority")
    release.add_argument("--triage-thread-id")
    release.add_argument("--metadata", help="JSON object")
    release.set_defaults(func=_cmd_release)
