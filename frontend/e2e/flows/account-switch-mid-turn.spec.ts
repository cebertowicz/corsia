import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createSession, openAppByUrl, waitForModelListLoaded } from '../support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const provider = 'codex';
const model = 'codex/e2e-account-switch';

type Seed = { first: string; second: string };
type Claim = { account_id: string; pending_account_id: string | null; turn_account_id: string | null };

function runFixture(script: string, threadId: string): string {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot) {
		throw new Error('Account-switch fixture requires isolated e2e-local mock state');
	}
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', script, threadId], {
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

function seedRunningHarnessTurn(threadId: string): Seed {
	return JSON.parse(runFixture(`
import json
import sys
from connections.account_evidence import mark_discovery
from connections.account_policy import select_account
from connections.account_receipts import capture_receipt
from connections.models import upsert_connection
from connections.provider_policy import effective_policy, set_policy
from connections.vendor_accounts import add_account, default_account, set_account_state
from infra.database import get_db

thread_id = sys.argv[1]
now = '2026-10-06T20:00:00+00:00'
upsert_connection(type='cli', provider='codex', status='available', config={'binary': '/bin/e2e-codex', 'app_server': True})
db = get_db()
try:
    db.execute("""INSERT OR REPLACE INTO models
       (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,enabled,available,
        context_window,supports_vision,supports_tools,supports_reasoning,default_max_tokens,created_at,updated_at)
       VALUES (?,?,?,?,?,?,?, 'e2e',1,1,128000,0,1,0,8192,?,?)""",
       ('codex/e2e-account-switch','codex','codex','e2e-account-switch','codex/e2e-account-switch',
        'E2E account switch','codex',now,now))
    first = default_account('codex', db=db)
    second = add_account('codex', 'Account B', db=db)
    second = set_account_state(second['id'], 'signed_in', expected_revision=second['revision'], db=db)
    for account in (first, second):
        receipt = capture_receipt('codex', account_id=account['id'], db=db)
        assert receipt is not None
        assert mark_discovery(account['id'], receipt=receipt, status='ready',
                              models={'codex/e2e-account-switch': {'available': True}}, db=db)
    project = db.execute('SELECT project_id FROM threads WHERE id=?', (thread_id,)).fetchone()['project_id']
    policy = effective_policy(project, 'codex', db=db)
    set_policy(project, 'codex', rollover=False, order=[first['id'], second['id']], disabled=[],
               expected_revision=policy['revision'], db=db)
    db.execute('UPDATE threads SET model=? WHERE id=?', ('codex/e2e-account-switch', thread_id))
    select_account(thread_id, 'codex', account_id=first['id'], model='codex/e2e-account-switch', db=db)
    db.execute("""INSERT INTO active_turns
        (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id)
        VALUES (?,?,0,?,'running',?,'e2e-running',?)""",
        (thread_id + ':running', thread_id, 'codex/e2e-account-switch', now, first['id']))
    db.commit()
    print(json.dumps({'first': first['id'], 'second': second['id']}))
finally:
    db.close()
`, threadId)) as Seed;
}

function finishAndClaim(threadId: string): Claim {
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
    db.commit()
    accept_thread_input(thread_id, role='user', content='Claim the pending account', intent='apply_now',
                        message_id=thread_id + ':next', db=db)
    claimed = start_idle_turn(thread_id, turn_id=thread_id + ':next-turn', boot_epoch='e2e-next',
                              started_at='2026-10-06T20:02:00+00:00', db=db)
    assert claimed is not None
    binding = db.execute('SELECT account_id,pending_account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?',
                         (thread_id, 'codex')).fetchone()
    turn = db.execute('SELECT vendor_account_id FROM active_turns WHERE id=?', (thread_id + ':next-turn',)).fetchone()
    db.execute("UPDATE active_turns SET status='completed', ended_at='2026-10-06T20:03:00+00:00' WHERE id=?", (thread_id + ':next-turn',))
    db.commit()
    print(json.dumps({'account_id': binding['account_id'], 'pending_account_id': binding['pending_account_id'],
                      'turn_account_id': turn['vendor_account_id']}))
finally:
    db.close()
`, threadId)) as Claim;
}

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await waitForModelListLoaded(page);
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}

async function screenshotPendingPicker(page: Page, testInfo: TestInfo): Promise<void> {
	for (const [width, theme] of [[1280, 'light'], [1280, 'dark'], [390, 'light'], [390, 'dark']] as const) {
		await page.setViewportSize({ width, height: 900 });
		await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
		await expect(page.locator('.model-sheet .switch-marker')).toHaveText('Switching…');
		await page.screenshot({ path: testInfo.outputPath(`account-switch-pending-${width}-${theme}.png`), fullPage: true, animations: 'disabled' });
	}
}

test('@flow:chat @surface:picker account choice during a running turn moves on the next claim', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'One controlled viewport matrix supplies the required visual evidence.');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state.');

	const title = `e2e account switch ${Date.now()}`;
	const threadId = await createSession(request, title);
	const { first, second } = seedRunningHarnessTurn(threadId);
	try {
		await page.setViewportSize({ width: 1280, height: 900 });
		await openThread(page, title);
		const accountB = page.locator('button.account-usage-row.picker', { hasText: 'Account B' });
		await expect(accountB).toBeEnabled();
		await expect(accountB).toHaveAttribute('aria-pressed', 'false');

		let puts = 0;
		page.on('request', (request) => {
			if (request.method() === 'PUT' && new URL(request.url()).pathname === `/api/threads/${threadId}/vendor-account`) puts += 1;
		});
		const [response] = await Promise.all([
			page.waitForResponse((response) => response.request().method() === 'PUT' && new URL(response.url()).pathname === `/api/threads/${threadId}/vendor-account`),
			accountB.click(),
		]);
		expect(response.status()).toBe(200);
		expect(await response.json()).toMatchObject({ account_id: second, changed: false, pending: true });
		expect(puts).toBe(1);
		await expect(accountB).toHaveAttribute('aria-pressed', 'true');
		await expect(accountB.locator('.switch-marker')).toHaveText('Switching…');

		const heldBinding = await request.get(`/api/threads/${threadId}/vendor-account?token=${process.env.CORSIA_E2E_TOKEN}`);
		expect(heldBinding.ok()).toBeTruthy();
		expect((await heldBinding.json()).accounts).toMatchObject([{ id: first }]);
		await screenshotPendingPicker(page, testInfo);

		const moved = finishAndClaim(threadId);
		expect(moved).toEqual({ account_id: second, pending_account_id: null, turn_account_id: second });
		await page.setViewportSize({ width: 1280, height: 900 });
		await page.reload();
		await page.locator('.thread-item').filter({ hasText: title }).click();
		await waitForModelListLoaded(page);
		await page.locator('button.model-chip').click();
		await expect(page.locator('button.account-usage-row.picker', { hasText: 'Account B' })).toHaveAttribute('aria-pressed', 'true');
		await expect(page.locator('.model-sheet .switch-marker')).toHaveCount(0);
	} finally {
		await request.delete(`/api/threads/${threadId}?token=${process.env.CORSIA_E2E_TOKEN}`);
	}
});
