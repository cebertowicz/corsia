"""Durable per-destination Codex link provenance; never read or copy auth bytes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import uuid

from connections.account_evidence import discovery_record, store_record
from connections.account_home import missing_home_reason, source_auth_exists, source_auth_path
from connections.account_policy import AccountAdmissionError
from connections.account_receipts import ReceiptFence, receipt_is_current
from connections.vendor_accounts import get_account
from infra.database import write_transaction
from infra.moved_state_paths import under_current
from infra.time import utc_now_iso

CHANGED = 'codex_auth_projection_changed'
SOURCE_MISSING = 'codex_auth_source_missing'
KINDS = frozenset({'live', 'discovery', 'checkpoint', 'detached'})


def _paths(account, home):
    # Resolve neither auth file nor final home: their disappearance/retarget is evidence.
    source = source_auth_path(account['source_home'])
    destination = Path(home).absolute() / 'auth.json'
    if source == destination:
        raise AccountAdmissionError('account_home_mismatch')
    return source, destination


def _safe_parents(path):
    return all(not parent.is_symlink() for parent in path.parents)


def _link_state(source, destination):
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return 'absent'
    if not _safe_parents(destination) or not stat.S_ISLNK(info.st_mode):
        return 'changed'
    return 'intact' if os.readlink(destination) == str(source) else 'changed'


def _current(receipt, *, db):
    if not receipt_is_current(receipt, db=db):
        raise AccountAdmissionError('account_login_unverified')
    account = get_account(receipt.account_id, db=db)
    if account['provider'] != 'codex' or account['source_home'] is None:
        raise AccountAdmissionError('account_home_mismatch')
    if account['state'] != 'signed_in':
        raise AccountAdmissionError('account_removed' if account['state'] == 'removed' else 'account_signed_out')
    return account, discovery_record(account)


def _gate(record, receipt):
    projection = record['auth_projection']
    failure = projection['failed']
    if failure and (failure.get('account_revision'), failure.get('login_seq')) == (
            receipt.account_revision, receipt.login_seq):
        return failure['cause']
    if any(item['phase'] == 'pending' for item in projection['stale']):
        return CHANGED
    return None


def _fail(account, record, receipt, cause, source, destination, *, db):
    if cause != SOURCE_MISSING:
        record['auth_projection']['failed'] = {
            'cause': cause, 'source': str(source), 'destination': str(destination),
            'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq, 'at': utc_now_iso()}
        store_record(account['id'], record, db=db)
    from connections.account_holds import refresh_account_holds

    refresh_account_holds(account['id'], db=db)


def repair_moved_projections(*, legacy_root: Path | None = None, state_root: Path | None = None, db=None) -> None:
    """Repair the legacy state-root move at startup; remove with heal_moved_state_paths."""
    from infra.config import STATE_ROOT
    from infra.platform_paths import legacy_state_root

    legacy = Path(legacy_root) if legacy_root is not None else legacy_state_root()
    current = str(Path(state_root if state_root is not None else STATE_ROOT).resolve(strict=False))
    prefixes = tuple(dict.fromkeys((str(legacy), str(legacy.resolve(strict=False)))))
    with write_transaction(db, scope='account_projection_move_repair') as connection:
        rows = connection.execute(
            "SELECT a.id FROM vendor_accounts a JOIN connections c ON c.id=a.connection_id "
            "WHERE c.provider='codex' AND a.discovery_json IS NOT NULL"
        ).fetchall()
        for row in rows:
            account = get_account(row['id'], db=connection)
            record = discovery_record(account)
            projection = record['auth_projection']
            before = repr(projection)
            failed = projection.get('failed')
            if isinstance(failed, dict):
                if failed.get('cause') == SOURCE_MISSING:
                    projection['failed'] = None
                else:
                    for field in ('source', 'destination'):
                        value = failed.get(field)
                        if isinstance(value, str):
                            failed[field] = under_current(value, prefixes, current) or value
            if current not in prefixes:
                _repair_moved_homes(projection, prefixes, current, account['revision'], record['login_seq'])
            if repr(projection) != before:
                store_record(account['id'], record, db=connection)
                from connections.account_holds import refresh_account_holds
                refresh_account_holds(account['id'], db=connection)


def _repair_moved_homes(projection, prefixes, current, revision, login_seq):
    for old_destination, entry in list(projection['homes'].items()):
        moved = under_current(old_destination, prefixes, current)
        if moved is None:
            for field in ('source', 'destination', 'home'):
                value = entry.get(field)
                if isinstance(value, str):
                    entry[field] = under_current(value, prefixes, current) or value
            continue
        old_source = entry['source']
        destination = Path(moved)
        state = _link_state(Path(old_source), destination)
        del projection['homes'][old_destination]
        if state == 'absent':
            continue
        if state == 'intact':  # no turn survives a restart, so even a temporary home's link is unowned
            destination.unlink()
            continue
        for field in ('source', 'destination', 'home'):
            value = entry.get(field)
            if isinstance(value, str):
                entry[field] = under_current(value, prefixes, current) or value
        projection['homes'].setdefault(moved, entry)  # a launch after the home repair may own it already
        if _link_state(Path(entry['source']), destination) == 'intact':
            continue  # already linked to the moved source
        failed = projection.get('failed')
        if state == 'changed' and (not failed or (failed.get('account_revision'), failed.get('login_seq')) != (
                revision, login_seq)):
            projection['failed'] = {
                'cause': CHANGED, 'source': entry['source'], 'destination': moved,
                'account_revision': revision, 'login_seq': login_seq, 'at': utc_now_iso()}


def projection_gate(receipt: ReceiptFence, *, db=None) -> None:
    """Account-wide gate before a caller even allocates a fresh temporary home."""
    with write_transaction(db, scope='account_projection_gate') as connection:
        account, record = _current(receipt, db=connection)
        cause = missing_home_reason(account) or _gate(record, receipt)
    if cause:
        raise AccountAdmissionError(cause)


def ensure_projection(receipt: ReceiptFence, *, home: Path, kind: str,
                      owner_turn_id: str | None = None, owner_boot_epoch: str | None = None, db=None) -> None:
    """Establish once or adopt an exact crash-left link; never repair unknown auth."""
    if kind not in KINDS:
        raise ValueError('unknown managed home kind')
    projection_gate(receipt, db=db)
    account = get_account(receipt.account_id, db=db)
    source, destination = _paths(account, home)
    cause = None
    stage = None
    # Directory work is outside the account writer; all path writers recheck below.
    if _safe_parents(destination) and source_auth_exists(source):
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = destination.parent / ('.corsia-auth-' + uuid.uuid4().hex)
        stage.symlink_to(source)
    try:
        with write_transaction(db, scope='account_projection_establish') as connection:
            account, record = _current(receipt, db=connection)
            source, destination = _paths(account, home)
            cause = _gate(record, receipt)
            entry = record['auth_projection']['homes'].get(str(destination))
            if cause is None:
                state = _link_state(source, destination)
                if not source_auth_exists(source):
                    cause = SOURCE_MISSING
                elif not _safe_parents(destination):
                    cause = CHANGED
                elif entry is not None:
                    if entry['phase'] != 'established' or (
                            entry['account_revision'], entry['login_seq'], entry['source']) != (
                            receipt.account_revision, receipt.login_seq, str(source)) or state != 'intact':
                        cause = CHANGED
                elif state == 'changed':
                    cause = CHANGED
                else:
                    if state == 'absent':
                        if stage is None:
                            cause = CHANGED
                        else:
                            os.rename(stage, destination)
                    if cause is None:
                        record['auth_projection']['homes'][str(destination)] = {
                            'source': str(source), 'destination': str(destination), 'home': str(destination.parent),
                            'kind': kind, 'account_revision': receipt.account_revision, 'login_seq': receipt.login_seq,
                            'phase': 'established', 'owner_turn_id': owner_turn_id, 'owner_boot_epoch': owner_boot_epoch,
                            'at': utc_now_iso()}
                        store_record(account['id'], record, db=connection)
                if cause:
                    _fail(account, record, receipt, cause, source, destination, db=connection)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
    if cause:
        raise AccountAdmissionError(cause)


def validate_projection(receipt: ReceiptFence, *, home: Path, db=None) -> None:
    """Missing established metadata/link is a durable refusal, including restart."""
    with write_transaction(db, scope='account_projection_validate') as connection:
        account, record = _current(receipt, db=connection)
        source, destination = _paths(account, home)
        cause = _gate(record, receipt)
        if cause is None:
            entry = record['auth_projection']['homes'].get(str(destination))
            if not source_auth_exists(source):
                cause = SOURCE_MISSING
            elif entry is None or entry['phase'] != 'established' or (
                    entry['account_revision'], entry['login_seq'], entry['source']) != (
                    receipt.account_revision, receipt.login_seq, str(source)) or _link_state(source, destination) != 'intact':
                cause = CHANGED
            if cause:
                _fail(account, record, receipt, cause, source, destination, db=connection)
    if cause:
        raise AccountAdmissionError(cause)


def begin_retirement(receipt: ReceiptFence, *, home: Path, owner_turn_id: str | None, db=None) -> bool:
    """Caller closes/joins first; commit retirement before removing its temporary home."""
    with write_transaction(db, scope='account_projection_retire') as connection:
        if not receipt_is_current(receipt, db=connection):
            return False
        account, record = _current(receipt, db=connection)
        source, destination = _paths(account, home)
        entry = record['auth_projection']['homes'].get(str(destination))
        if entry is None or entry['kind'] == 'live' or entry['owner_turn_id'] != owner_turn_id:
            return False
        if entry['phase'] == 'established' and _link_state(source, destination) != 'intact':
            _fail(account, record, receipt, CHANGED, source, destination, db=connection)
            return False
        entry['phase'] = 'retiring'
        store_record(account['id'], record, db=connection)
        return True


def complete_retirement(receipt: ReceiptFence, *, home: Path, owner_turn_id: str | None, db=None) -> bool:
    with write_transaction(db, scope='account_projection_retired') as connection:
        if not receipt_is_current(receipt, db=connection):
            return False
        account, record = _current(receipt, db=connection)
        _, destination = _paths(account, home)
        entry = record['auth_projection']['homes'].get(str(destination))
        if entry is None or entry['phase'] != 'retiring' or entry['owner_turn_id'] != owner_turn_id:
            return False
        if destination.parent.exists() or destination.is_symlink():
            return False
        del record['auth_projection']['homes'][str(destination)]
        store_record(account['id'], record, db=connection)
        return True


def cleanup_stale(receipt: ReceiptFence, *, quiesced: bool, db=None) -> bool:
    """Explicit fresh-login cleanup only, serialized against every projection writer."""
    if not quiesced:
        return False
    with write_transaction(db, scope='account_projection_login_cleanup') as connection:
        if not receipt_is_current(receipt, db=connection):
            return False
        account, record = _current(receipt, db=connection)
        source = str(source_auth_path(account['source_home']))
        for item in record['auth_projection']['stale']:
            if item['phase'] != 'pending':
                continue
            destination = Path(item['destination'])
            if (item['cleanup_revision'], item['cleanup_login_seq'], item['source']) != (
                    receipt.account_revision, receipt.login_seq, source) or (
                    destination != Path(item['home']) / 'auth.json' or str(destination) == source
                    or not _safe_parents(destination)):
                return False
            try:
                destination.unlink(missing_ok=True)  # Unlink the owned path; never follow its target.
            except OSError:
                return False
            item['phase'] = 'done'
        record['auth_projection']['stale'] = []
        store_record(account['id'], record, db=connection)
        return True
