from __future__ import annotations

import stat
import sys
import textwrap

import pytest

_FAKE_CRASH = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    sys.stdout.write(json.dumps({{"type":"system","subtype":"init","session_id":"f","model":"m","tools":[],"mcp_servers":[],"cwd":"."}}) + "\\n")
    sys.stdout.flush()
    sys.stdin.readline()
    sys.stderr.write("Error: Invalid session ID. Must be a valid UUID.\\n")
    sys.stderr.flush()
    sys.exit(1)
    '''
)

_FAKE_ONESHOT = textwrap.dedent(
    '''\
    #!{python}
    import sys, json
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        if marker in content:
            return content.split(marker, 1)[1]
        return content
    emit({{"type":"system","subtype":"init","session_id":"f","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    line = sys.stdin.readline()
    if line.strip():
        c = visible_content(json.loads(line).get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"ok","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
	    '''
)

_FAKE_REJECT_RESUME_ONCE = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys
    count_path = sys.argv[0] + ".count"
    try:
        with open(count_path) as f:
            count = int(f.read().strip() or "0")
    except FileNotFoundError:
        count = 0
    with open(count_path, "w") as f:
        f.write(str(count + 1))
    if "--resume" in sys.argv and count == 0:
        sid = sys.argv[sys.argv.index("--resume") + 1]
        sys.stderr.write("No conversation found with session ID: " + sid + "\\n")
        sys.stderr.flush()
        sys.exit(1)
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        if marker in content:
            return content.split(marker, 1)[1]
        return content
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        c = visible_content(json.loads(line).get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"ok","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)

_FAKE_REJECT_RESUME_DELAYED_ONCE = textwrap.dedent(
    '''\
    #!{python}
    import json, os, sys, time
    count_path = sys.argv[0] + ".count"
    try:
        with open(count_path) as f:
            count = int(f.read().strip() or "0")
    except FileNotFoundError:
        count = 0
    with open(count_path, "w") as f:
        f.write(str(count + 1))
    if "--resume" in sys.argv and count == 0:
        sid = sys.argv[sys.argv.index("--resume") + 1]
        # Reject around the provider write rather than at spawn.
        time.sleep(0.2)
        sys.stderr.write("No conversation found with session ID: " + sid + "\\n")
        sys.stderr.flush()
        sys.exit(1)
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        if marker in content:
            return content.split(marker, 1)[1]
        return content
    emit({{"type":"system","subtype":"init","session_id":"fake","model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        c = visible_content(json.loads(line).get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"ok","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)

# Claude rejects the dormant resume only after the admitted input has been
# written to stdin, as a real dormant thread does (issue #2412's stall).
_FAKE_REJECT_RESUME_AFTER_WINDOW_ONCE = _FAKE_REJECT_RESUME_DELAYED_ONCE.replace(
    "time.sleep(0.2)", "time.sleep(2.0)",
)

_FAKE_RESUME_HANGS = textwrap.dedent(
    '''\
    #!{python}
    import time
    while True:
        time.sleep(1)
    '''
)

_FAKE_INIT_THEN_REJECT_RESUME = textwrap.dedent(
    '''\
    #!{python}
    import json, sys
    sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "?"
    sys.stdout.write(json.dumps({{"type":"system","subtype":"init","session_id":sid,"model":"m","tools":[],"mcp_servers":[],"cwd":"."}}) + "\\n")
    sys.stdout.flush()
    sys.stdin.readline()
    sys.stderr.write("No conversation found with session ID: " + sid + "\\n")
    sys.stderr.flush()
    sys.exit(1)
    '''
)

_FAKE_SYNTHETIC_RESUME_NEEDS_FAST_FEED = textwrap.dedent(
    '''\
    #!{python}
    import json, select, sys, time
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    def visible_content(content):
        marker = "User message:\\n"
        if marker in content:
            return content.split(marker, 1)[1]
        return content
    deadline = time.time() + 0.5
    while time.time() < deadline:
        ready, _, _ = select.select([sys.stdin], [], [], max(0, deadline - time.time()))
        if not ready:
            continue
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        c = visible_content(json.loads(line).get("message", {{}}).get("content", ""))
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"echo:" + c}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"ok","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
        deadline = time.time() + 0.5
    sys.exit(0)
    '''
)


@pytest.fixture
def fake_claude_crash(tmp_path):
    p = tmp_path / "fake_claude_crash"
    p.write_text(_FAKE_CRASH.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_oneshot(tmp_path):
    p = tmp_path / "fake_claude_oneshot_main"
    p.write_text(_FAKE_ONESHOT.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_reject_resume_once(tmp_path):
    p = tmp_path / "fake_claude_reject_resume_once"
    p.write_text(_FAKE_REJECT_RESUME_ONCE.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_reject_resume_delayed_once(tmp_path):
    p = tmp_path / "fake_claude_reject_resume_delayed_once"
    p.write_text(_FAKE_REJECT_RESUME_DELAYED_ONCE.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_reject_resume_after_window_once(tmp_path):
    p = tmp_path / "fake_claude_reject_resume_after_window_once"
    p.write_text(_FAKE_REJECT_RESUME_AFTER_WINDOW_ONCE.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_synthetic_resume_needs_fast_feed(tmp_path):
    p = tmp_path / "fake_claude_synthetic_resume_needs_fast_feed"
    p.write_text(_FAKE_SYNTHETIC_RESUME_NEEDS_FAST_FEED.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_resume_hangs(tmp_path):
    p = tmp_path / "fake_claude_resume_hangs"
    p.write_text(_FAKE_RESUME_HANGS.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# A Claude that resumed a transcript holding an empty assistant text block: it
# reads the input, then the API rejects the replayed request (#1138).
_FAKE_POISONED_RESUME = textwrap.dedent(
    '''\
    #!{python}
    import json, sys
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "?"
    emit({{"type":"system","subtype":"init","session_id":sid,"model":"m","tools":[],"mcp_servers":[],"cwd":"."}})
    for line in sys.stdin:
        if not line.strip():
            continue
        emit({{"type":"result","subtype":"success","is_error":True,"result":"API Error: 400 messages: text content blocks must be non-empty","total_cost_usd":0.0,"num_turns":1,"stop_reason":"stop_sequence"}})
    '''
)


@pytest.fixture
def fake_claude_poisoned_resume(tmp_path):
    p = tmp_path / "fake_claude_poisoned_resume"
    p.write_text(_FAKE_POISONED_RESUME.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


# A Claude whose CORSIA MCP server reports ready with no tools (the bridge
# refused its list call), and which would happily answer toolless (#1137).
_FAKE_TOOLLESS_BRIDGE = textwrap.dedent(
    '''\
    #!{python}
    import json, sys
    def emit(o):
        sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    emit({{"type":"system","subtype":"init","session_id":"f","model":"m","tools":[],"mcp_servers":[{{"name":"corsia","status":"connected"}}],"cwd":"."}})
    for line in sys.stdin:
        if not line.strip():
            continue
        emit({{"type":"assistant","message":{{"content":[{{"type":"text","text":"toolless answer"}}]}}}})
        emit({{"type":"result","subtype":"success","is_error":False,"result":"ok","total_cost_usd":0.0,"num_turns":1,"stop_reason":"end_turn"}})
    '''
)


@pytest.fixture
def fake_claude_toolless_bridge(tmp_path):
    p = tmp_path / "fake_claude_toolless_bridge"
    p.write_text(_FAKE_TOOLLESS_BRIDGE.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)


@pytest.fixture
def fake_claude_init_then_reject_resume(tmp_path):
    p = tmp_path / "fake_claude_init_then_reject_resume"
    p.write_text(_FAKE_INIT_THEN_REJECT_RESUME.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(p)
