import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { TEST_TOKEN, createSession, openAppByUrl, waitForModelListLoaded } from '../support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const codexModel = 'codex/e2e-cross-vendor';
const claudeModel = 'claude-code/e2e-cross-vendor';

type Seed = { codex: string; first: string; second: string };
type Snapshot = { codex: unknown; thread: unknown; turns: number };
type Claim = { claude: [string, string | null]; turn: string | null };

function runFixture(script: string, ...args: string[]): string {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot) {
		throw new Error('Cross-vendor account fixture requires isolated e2e-local mock state');
	}
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', script, ...args], {
		cwd: repoRoot,
		encoding: 'utf8',
		env: {
			...process.env,
			PYTHONPATH: path.join(repoRoot, 'backend'),
			CORSIA_WORKSPACE: workspace,
			CORSIA_STATE_DIR: stateRoot,
			CORSIA_TEST_MODE: '1',
			CORSIA_ANALYTICS_DISABLE: '1',
		},
	});
}

// A thread running Codex with a live Codex turn, two Claude Code accounts, and (case B) a Claude Code binding.
function seedCodexThread(threadId: string, suffix: string, bindClaude: boolean): Seed {
	return JSON.parse(runFixture(`
import json
import sys
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, default_account, set_account_state
from infra.database import get_db

thread_id, suffix, bind_claude = sys.argv[1], sys.argv[2], sys.argv[3] == '1'
now = '2026-10-06T20:00:00+00:00'
upsert_connection(type='cli', provider='codex', status='available', config={'binary': '/bin/e2e-codex', 'app_server': True})
upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/e2e-claude'})
db = get_db()
try:
    for model_id, harness in (('codex/e2e-cross-vendor', 'codex'), ('claude-code/e2e-cross-vendor', 'claude-code')):
        db.execute("""INSERT OR REPLACE INTO models
           (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,enabled,available,
            context_window,supports_vision,supports_tools,supports_reasoning,default_max_tokens,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?, 'e2e',1,1,128000,0,1,0,8192,?,?)""",
           (model_id, harness, harness, 'e2e-cross-vendor', model_id,
            'E2E cross vendor ' + harness, harness, now, now))
    codex = default_account('codex', db=db)
    first = add_account('claude-code', 'Claude First ' + suffix, db=db)
    second = add_account('claude-code', 'Claude Second ' + suffix, db=db)
    first = set_account_state(first['id'], 'signed_in', expected_revision=first['revision'], db=db)
    second = set_account_state(second['id'], 'signed_in', expected_revision=second['revision'], db=db)
    for account, model_id in ((codex, 'codex/e2e-cross-vendor'), (first, 'claude-code/e2e-cross-vendor'), (second, 'claude-code/e2e-cross-vendor')):
        receipt = capture_receipt(account['provider'], account_id=account['id'], db=db)
        assert receipt is not None
        assert mark_discovery(account['id'], receipt=receipt, status='ready', models={model_id: {'available': True}}, db=db)
    db.execute('UPDATE threads SET model=? WHERE id=?', ('codex/e2e-cross-vendor', thread_id))
    select_account(thread_id, 'codex', account_id=codex['id'], model='codex/e2e-cross-vendor', db=db)
    if bind_claude:
        select_account(thread_id, 'claude-code', account_id=first['id'], model='claude-code/e2e-cross-vendor', db=db)
    db.execute("""INSERT INTO active_turns
        (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id)
        VALUES (?,?,0,?,'running',?,'e2e-running',?)""",
        (thread_id + ':running', thread_id, 'codex/e2e-cross-vendor', now, codex['id']))
    db.commit()
    print(json.dumps({'codex': codex['id'], 'first': first['id'], 'second': second['id']}))
finally:
    db.close()
`, threadId, suffix, bindClaude ? '1' : '0')) as Seed;
}

// Everything the Codex side owns: its binding, the thread's epoch/session fields and its active turns.
function snapshot(threadId: string): Snapshot {
	return JSON.parse(runFixture(`
import json
import sys
from infra.database import get_db

thread_id = sys.argv[1]
db = get_db()
try:
    codex = db.execute('SELECT * FROM thread_vendor_accounts WHERE thread_id=? AND provider=?', (thread_id, 'codex')).fetchone()
    thread = db.execute('SELECT * FROM threads WHERE id=?', (thread_id,)).fetchone()
    keep = [k for k in thread.keys() if 'epoch' in k or 'session' in k or k in ('model', 'lifecycle_state')]
    turns = db.execute("SELECT COUNT(*) FROM active_turns WHERE thread_id=? AND status='running'", (thread_id,)).fetchone()[0]
    print(json.dumps({'codex': dict(codex) if codex else None, 'thread': {k: thread[k] for k in keep}, 'turns': turns}, default=str))
finally:
    db.close()
`, threadId)) as Snapshot;
}

function claudeBinding(threadId: string): [string, string | null] | null {
	return JSON.parse(runFixture(`
import json
import sys
from infra.database import get_db

db = get_db()
try:
    row = db.execute('SELECT account_id,pending_account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
                     (sys.argv[1], 'claude-code')).fetchone()
    print(json.dumps([row['account_id'], row['pending_account_id']] if row else None))
finally:
    db.close()
`, threadId)) as [string, string | null] | null;
}

// Mock mode has no Claude Code harness to run a turn on, so the thread's next claim on Claude Code is
// driven through the same admission and claim code a sent message reaches.
function claimOnClaudeCode(threadId: string): Claim {
	return JSON.parse(runFixture(`
import json
import sys
from infra.database import get_db
from infra.input_admission import accept_thread_input
from infra.thread_inputs import start_idle_turn

thread_id = sys.argv[1]
db = get_db()
try:
    db.execute("UPDATE active_turns SET status='completed', ended_at='2026-10-06T20:01:00+00:00' WHERE id=?", (thread_id + ':running',))
    db.execute('UPDATE threads SET model=? WHERE id=?', ('claude-code/e2e-cross-vendor', thread_id))
    db.commit()
    accept_thread_input(thread_id, role='user', content='Run on Claude Code', intent='apply_now',
                        message_id=thread_id + ':next', db=db)
    claimed = start_idle_turn(thread_id, turn_id=thread_id + ':next-turn', boot_epoch='e2e-next',
                              started_at='2026-10-06T20:02:00+00:00', db=db)
    assert claimed is not None
    binding = db.execute('SELECT account_id,pending_account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
                         (thread_id, 'claude-code')).fetchone()
    turn = db.execute('SELECT vendor_account_id FROM active_turns WHERE id=?', (thread_id + ':next-turn',)).fetchone()
    db.execute("UPDATE active_turns SET status='completed', ended_at='2026-10-06T20:03:00+00:00' WHERE id=?", (thread_id + ':next-turn',))
    db.commit()
    print(json.dumps({'claude': [binding['account_id'], binding['pending_account_id']], 'turn': turn['vendor_account_id']}))
finally:
    db.close()
`, threadId)) as Claim;
}

// Accounts sit behind each vendor header and start collapsed (model-first-picker-renders.spec.ts asserts
// aria-expanded="false"); open every vendor's chooser so its account rows can be picked.
async function expandAccountChoosers(page: Page): Promise<void> {
	// The accounts load after the sheet opens, so wait for the first chooser before clicking.
	await expect(page.locator('.model-sheet button.picker-account-title').first()).toBeVisible();
	const closed = page.locator('.model-sheet button.picker-account-title[aria-expanded="false"]');
	// Each click removes a row from this live selector, so always click the first one left.
	while (await closed.count() > 0) await closed.first().click();
}

async function openPicker(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await waitForModelListLoaded(page);
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
	await expandAccountChoosers(page);
}

async function choose(page: Page, threadId: string, name: string, expectChanged: boolean): Promise<void> {
	const row = page.locator('button.account-usage-row.picker', { hasText: name });
	await expect(row).toBeEnabled();
	await expect(row).toHaveAttribute('aria-pressed', 'false');
	const [response] = await Promise.all([
		page.waitForResponse((r) => r.request().method() === 'PUT' && new URL(r.url()).pathname === `/api/threads/${threadId}/vendor-account`),
		row.click(),
	]);
	expect(response.status(), await response.text()).toBe(200);
	expect(await response.json()).toMatchObject({ pending: true, changed: expectChanged });
	await expect(page.getByText("Couldn't switch accounts")).toHaveCount(0);
	await expect(row).toHaveAttribute('aria-pressed', 'true');
}

async function screenshotPicker(page: Page, testInfo: TestInfo, label: string): Promise<void> {
	const dir = process.env.CORSIA_E2E_SHOT_DIR;
	for (const [width, theme] of [[1280, 'light'], [1280, 'dark'], [390, 'light'], [390, 'dark']] as const) {
		await page.setViewportSize({ width, height: 900 });
		await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
		await expect(page.locator('.model-sheet')).toBeVisible();
		const file = `${label}-${width}-${theme}.png`;
		await page.screenshot({ path: dir ? path.join(dir, `${testInfo.project.name}-${file}`) : testInfo.outputPath(file), fullPage: true, animations: 'disabled' });
	}
}

for (const bindClaude of [false, true]) {
	const label = bindClaude ? 'held-for-existing-binding' : 'bound-without-binding';
	test(`@flow:chat @surface:picker a Claude Code account chosen on a Codex thread is accepted: ${label}`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state.');

		const suffix = `${testInfo.project.name}-${label}-${Date.now()}`;
		const title = `e2e cross vendor ${suffix}`;
		const threadId = await createSession(request, title);
		const { codex, first, second } = seedCodexThread(threadId, suffix, bindClaude);
		try {
			const before = snapshot(threadId);
			expect(claudeBinding(threadId)).toEqual(bindClaude ? [first, null] : null);

			await openPicker(page, title);
			// Another vendor's account chosen while the thread runs Codex: written when the vendor has no
			// binding yet (changed), held for its first claim otherwise.
			await choose(page, threadId, `Claude ${bindClaude ? 'Second' : 'First'} ${suffix}`, !bindClaude);
			const chosen = bindClaude ? second : first;

			expect(claudeBinding(threadId)).toEqual(bindClaude ? [first, second] : [first, null]);
			expect(snapshot(threadId)).toEqual(before);
			await screenshotPicker(page, testInfo, label);

			// The mark survives a reload, and the Codex side is still untouched.
			await page.setViewportSize({ width: 1280, height: 900 });
			await page.reload();
			if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.locator('.thread-item').filter({ hasText: title }).click();
			await waitForModelListLoaded(page);
			await page.locator('button.model-chip').click();
			await expandAccountChoosers(page);
			await expect(page.locator('button.account-usage-row.picker', { hasText: `Claude ${bindClaude ? 'Second' : 'First'} ${suffix}` })).toHaveAttribute('aria-pressed', 'true');
			expect(snapshot(threadId)).toEqual(before);

			// The thread's first turn on Claude Code runs on the chosen account.
			const claim = claimOnClaudeCode(threadId);
			expect(claim).toEqual({ claude: [chosen, null], turn: chosen });
			expect(before.codex).toMatchObject({ account_id: codex });
		} finally {
			await request.delete(`/api/threads/${threadId}?token=${TEST_TOKEN}`);
		}
	});
}
