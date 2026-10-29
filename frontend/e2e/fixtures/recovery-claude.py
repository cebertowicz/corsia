#!/usr/bin/env python3
"""Disposable Claude CLI for the unchecked-to-ready browser proof."""
import json
import os
from pathlib import Path
import sys

home = Path(os.environ.get('CLAUDE_CONFIG_DIR', os.environ['CORSIA_STATE_DIR'] + '/default-claude'))
args = sys.argv[1:]
with (Path(os.environ['CORSIA_STATE_DIR']) / 'recovery-cli.log').open('a') as log:
    log.write(json.dumps({'args': args, 'home': str(home)}) + '\n')
if args == ['--version']:
    print('2.1.287 (Claude Code)')
    sys.exit(0)
if args == ['auth', 'logout']:
    (home / 'signed').unlink(missing_ok=True)
    sys.exit(0)
if args == ['auth', 'login']:
    home.mkdir(parents=True, exist_ok=True)
    (home / 'signed').touch()
    sys.exit(0)
if args == ['auth', 'status']:
    signed = (home / 'signed').exists()
    print(json.dumps({'loggedIn': signed, 'authMethod': 'claude.ai' if signed else 'none',
                      'apiProvider': 'firstParty', 'email': 'recovery@example.test',
                      'orgId': 'synthetic-org', 'orgName': 'Synthetic', 'subscriptionType': 'team'}))
    sys.exit(0 if signed else 1)
if '-p' in args:
    marker = Path(os.environ['CORSIA_STATE_DIR']) / 'recovery-first-check-failed'
    for line in sys.stdin:
        frame = json.loads(line)
        req = frame['request']
        if req['subtype'] == 'list_models' and os.environ.get('CLAUDE_CONFIG_DIR') and not marker.exists():
            marker.touch()
            sys.exit(1)
        payload = {}
        if req['subtype'] == 'list_models':
            payload = {'models': [{'value': 'sonnet', 'displayName': 'Sonnet', 'resolvedModel': 'claude-sonnet-synthetic'}]}
        if req['subtype'] == 'get_context_usage':
            payload = {'contextWindow': 200000}
        print(json.dumps({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': frame['request_id'], 'response': payload}}), flush=True)
    sys.exit(0)
sys.exit(3)
