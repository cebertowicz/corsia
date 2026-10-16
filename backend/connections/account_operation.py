"""Checkpoint and detached operations reuse the canonical account admission owner."""
from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path

from connections.account_policy import require_claim, require_launch
from connections.account_receipts import capture_receipt
from connections.account_runtime import OPERATION_KINDS, launch_homes
from infra.database import get_db


def admit_operation(provider, model, request, *, actor_thread_id=None):
    db = get_db()
    try:
        if request.vendor_launch is not None:
            require_claim(provider, thread_id=request.thread_id, model=model,
                actor_thread_id=actor_thread_id, account_id=request.vendor_account_id, frozen=True, db=db)
            identity = request.vendor_account_id
            current = capture_receipt(provider, account_id=identity, db=db) if identity is not None else None
            if current != request.vendor_account_receipt:
                from connections.account_policy import AccountAdmissionError

                raise AccountAdmissionError('account_login_unverified')
            return request
        identity = require_claim(provider, thread_id=request.thread_id, model=model,
            actor_thread_id=actor_thread_id, account_id=request.vendor_account_id,
            frozen=request.vendor_account_bound, db=db)
        launch = launch_homes(identity, db=db)
        if not launch.custom:
            from connections.account_profiles import profile_for

            launch = replace(launch, warm_home=profile_for(provider).default_warm_home())
        receipt = capture_receipt(provider, account_id=identity, db=db) if identity is not None else None
        return replace(request, vendor_account_id=identity, vendor_account_bound=True,
            vendor_account_receipt=receipt, vendor_launch=launch)
    finally:
        db.close()


def require_operation(provider, model, request, *, actor_thread_id=None, db=None):
    return require_launch(provider, thread_id=request.thread_id, model=model,
        actor_thread_id=actor_thread_id, account_id=request.vendor_account_id, frozen=True,
        launch=request.vendor_launch, receipt=request.vendor_account_receipt, db=db)


def prepare_operation_home(request, home: Path, *, kind, model=None):
    """A current receipt must pass admission before any first managed projection."""
    launch = request.vendor_launch
    if launch.custom:
        from connections.account_projection import ensure_projection
        from connections.account_policy import require_claim

        require_claim('codex', thread_id=request.thread_id, model=_model(request, model),
            account_id=request.vendor_account_id, frozen=True,
            actor_thread_id=getattr(request, 'actor_thread_id', None))
        ensure_projection(request.vendor_account_receipt, home=home, kind=kind)
    return replace(request, vendor_launch=replace(launch, codex_runtime=str(home), isolated=True))


def _model(request, model=None):
    return model if model is not None else request.target.catalog_id


def bind_operation_session(request, session, *, kind, model=None):
    if kind not in OPERATION_KINDS:
        raise ValueError(f'unknown operation kind: {kind}')
    launch = request.vendor_launch
    session.vendor_account_id = request.vendor_account_id
    session.vendor_account_receipt = request.vendor_account_receipt
    session.vendor_account_bound = True
    session.vendor_account_custom = launch.custom
    session.vendor_operation_kind = kind
    session.vendor_warm_home = launch.warm_home
    session.vendor_catalog_model = _model(request, model)
    if launch.custom:
        from connections.account_evidence import discovery_record
        from connections.vendor_accounts import get_account

        cache = discovery_record(get_account(request.vendor_account_id))['discovery']['cache_receipt']
        session.vendor_cache_receipt = cache
        session.vendor_catalog_source = Path(cache['path']) if cache else None
    if session.vendor_catalog_model.startswith('codex/'):
        session.source_codex_home = launch.codex_source or launch.warm_home
        session.codex_home = launch.codex_runtime
    else:
        session.claude_dir = launch.claude_dir
    return session


def require_same_operation_home(request, source_home):
    """Refuse a warm source belonging to another account through the existing cold owner."""
    if request.warm_source is None:
        return
    launch = request.vendor_launch
    expected = launch.claude_dir or launch.codex_runtime or launch.warm_home
    if source_home is None and not launch.custom:
        source_home = launch.warm_home
    if source_home is None or Path(source_home).absolute() != Path(expected).absolute():
        _refuse_warm_source('warm_fork_home_mismatch')
    receipt = getattr(request, 'vendor_account_receipt', None)
    if getattr(request.warm_source, 'vendor_account_receipt', None) != receipt:
        _refuse_warm_source('warm_fork_login_mismatch')


def _refuse_warm_source(reason):
    from core.checkpoint_types import WarmForkUnusable

    raise WarmForkUnusable(reason, reason=reason)


@contextmanager
def operation_directory(request, *, prefix, kind, home_subdir=None, model=None):
    """Existing temporary lifetime with durable intentional retirement on either outcome."""
    import tempfile
    from connections.account_projection import begin_retirement, complete_retirement

    retired, home = False, None
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as raw:
            # Canonicalize the new directory, never the credential link itself.
            root = Path(raw).resolve()
            home = root / home_subdir if home_subdir else root
            home.mkdir(exist_ok=True)
            request = prepare_operation_home(request, home, kind=kind, model=model)
            try:
                yield request, root
            finally:
                if request.vendor_launch.custom:
                    retired = begin_retirement(request.vendor_account_receipt, home=home, owner_turn_id=None)
    finally:
        if retired:
            complete_retirement(request.vendor_account_receipt, home=home, owner_turn_id=None)
