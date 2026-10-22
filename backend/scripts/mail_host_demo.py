"""Walk the other-mail-hosts main path end to end against local fake servers and disposable state.

    cd backend && PYTHONPATH=. ../.venv/bin/python scripts/mail_host_demo.py

Fake IMAP, SMTP and CalDAV servers listen on 127.0.0.1 behind TLS signed by a throwaway CA. The CA is
trusted only by this process, through ``SSL_CERT_FILE``; CORSIA's own TLS context is untouched. State is a
temporary directory removed on exit. One plain line per step; the app password is never printed.
Not imported by the app. Needs the test helpers beside it (``backend/tests``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="corsia-mail-host-demo-"))
CA_DIR = ROOT / "ca"
CA_DIR.mkdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
os.environ.update(CORSIA_WORKSPACE=str(ROOT / "workspace"), CORSIA_STATE_DIR=str(ROOT / "state"),
                  CORSIA_TEST_MODE="1", CORSIA_ANALYTICS_DISABLE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
(ROOT / "workspace").mkdir()
(ROOT / "state").mkdir()

from mailhost_fakes import SAMPLE_EVENTS, FakeCalDav, FakeImap, FakeSmtp, make_authority, message  # noqa: E402

AUTHORITY = make_authority(CA_DIR)
os.environ["SSL_CERT_FILE"] = str(AUTHORITY.ca_file)  # trusted by this process only; the app's context is the default one

import infra.config as config  # noqa: E402
import infra.database as database  # noqa: E402

ADDRESS = "demo@mail.example"
APP_PASSWORD = "demo-app-password-0123456789ab"
LINES: list[str] = []


def say(text: str) -> None:
    LINES.append(text)
    print(f"[{len(LINES):02d}] {text}")


def bootstrap_state() -> None:
    """A fresh disposable database, the same way the test fixtures make one."""
    import sqlite3

    from auth_fixtures import seed_test_accounts

    path = ROOT / "demo.db"

    def reconfigure() -> None:
        config.DB_PATH = path
        database.DB_PATH = path
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        database.initialize_schema(conn)
        with conn:
            seed_test_accounts(conn)
        conn.close()

    config.STATE_ROOT = ROOT / "state"
    database.shutdown_pool(reconfigure)


def make_corsia_thread() -> str:
    """The designated CORSIA thread; only it may call ``mail_host_connect``."""
    from datetime import datetime, timezone

    from infra.corsia_agent import set_corsia_agent

    db = database.get_db()
    thread_id, now = "demo-corsia-thread", datetime.now(timezone.utc).isoformat()
    with database.write_transaction(db, scope="mail_host_demo") as conn:
        conn.execute("INSERT INTO threads (id,user_id,title,project_id,model,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                     (thread_id, "steve", "demo", None, "native/test", now, now))
    assert set_corsia_agent(thread_id, db=db)
    return thread_id


async def main(thread: str) -> int:
    from connections import mail_accounts  # noqa: F401  (loads the resolver the tools use)
    from connections.mail import email_screen
    from connections.mailhost import check, store
    from infra import key_store
    from infra.db_async import run_in_db
    from tools.google_calendar import CalendarListTool
    from tools.google_email_read import EmailReadTool, EmailSearchTool
    from tools.mail_host_connect import MailHostConnectTool

    login = {"user": ADDRESS, "password": APP_PASSWORD}
    imap = FakeImap(AUTHORITY, messages={1: message("Lunch plans", "Noon on Friday?"),
                                         2: message("Invoice 42", "Please pay by the 30th.", sender="billing@example.com")}, **login)
    smtp = FakeSmtp(AUTHORITY, **login)
    caldav = FakeCalDav(AUTHORITY, events=SAMPLE_EVENTS, **login)
    say(f"fake IMAP :{imap.port}, SMTP :{smtp.port}, CalDAV :{caldav.port} are up behind TLS from a throwaway CA")

    async def screened(*_args, **_kwargs):  # the local screen model isn't part of this demo
        return email_screen.Screen(status=email_screen.STATUS_SCREENED, kind="other", needs_person="later")

    email_screen.screen_message = screened  # type: ignore[assignment]

    # 1. The agent adds the account: an address, and servers only because this host isn't in the known table.
    added = json.loads(await MailHostConnectTool().execute(
        _thread_id=thread, address=ADDRESS, imap=f"127.0.0.1:{imap.port}", smtp=f"127.0.0.1:{smtp.port}", caldav=caldav.url))
    account_id = added["account_id"]
    say(f"agent added {ADDRESS}: status {added['status']}, password goes to {', '.join(added['password_goes_to'])}")
    say(f"agent was told: {added['next'][:90]}...")

    # 2. A second call for the same address changes nothing.
    db = database.get_db()
    before = store.read(account_id, db=db)[0]
    again = json.loads(await MailHostConnectTool().execute(_thread_id=thread, address=ADDRESS.upper(), imap="other.example"))
    same = before == store.read(account_id, db=db)[0]
    say(f"second call: same account {again['account_id'] == account_id}, already existed {again['already_existed']}, servers unchanged {same}")

    # 3. The person pastes the app password in Settings > Keys; the check runs from that write.
    await run_in_db(key_store.set_value, added["key_name"], APP_PASSWORD)
    await check.start_after_key_write(added["key_name"])
    say("person set the key; the row shows Checking")
    await check.wait_for_checks()
    state = db.execute("SELECT state, capabilities_json FROM connected_accounts WHERE id=?", (account_id,)).fetchone()
    status = store.read(account_id, db=db)[1]["status"]
    say(f"check finished: {status}, row {state['state']}, can do {json.loads(state['capabilities_json'])}")

    # 4. The agent uses it.
    found = json.loads(await EmailSearchTool().execute(_thread_id=thread, query="newer_than:30d"))["messages"]
    say(f"email_search found {len(found)} messages: " + "; ".join(f"{m['message_id']} from {m['from']}" for m in found))
    opened = json.loads(await EmailReadTool().execute(_thread_id=thread, message_id=found[0]["message_id"]))
    say(f"email_read {opened['message_id']}: subject '{opened['subject']}', body {len(opened['body'])} chars inside untrusted-email tags")
    events = json.loads(await CalendarListTool().execute(
        _thread_id=thread, time_min="2026-10-22T00:00:00-07:00", time_max="2026-10-28T00:00:00-07:00"))["events"]
    say(f"calendar_list found {len(events)} events: " + ", ".join(event["title"] for event in events))

    # 5. The host stops accepting the password: agents stop using the account.
    imap.password = "revoked-at-the-host"
    refused = await EmailSearchTool().execute(_thread_id=thread, query="x")
    say(f"after the host revoked the password: {refused[:100]}")
    again_refused = await EmailSearchTool().execute(_thread_id=thread, query="x")
    say(f"the next call doesn't reach the host: {again_refused[:100]}")
    plain_login_bytes = imap.plain + smtp.plain
    say(f"no byte reached any server outside TLS carrying the password: {APP_PASSWORD.encode() not in plain_login_bytes}")

    for fake in (imap, smtp, caldav):
        fake.close()
    if any(APP_PASSWORD in line for line in LINES):
        print("FAILED: the app password appeared in this script's own output", file=sys.stderr)
        return 1
    say("done; the app password appeared in no line above")
    return 0


if __name__ == "__main__":
    try:
        bootstrap_state()
        code = asyncio.run(main(make_corsia_thread()))
    finally:
        import shutil

        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(code)
