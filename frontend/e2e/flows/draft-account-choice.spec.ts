import { test } from '../support/test';
import { expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { openAppByUrl } from '../support/corsia';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
function fixture(script: string, ...args: string[]): string {
	if (process.env.CORSIA_E2E_MOCK !== '1' || !process.env.CORSIA_E2E_WORKSPACE || !process.env.CORSIA_E2E_STATE_DIR) throw new Error('Disposable mock instance required');
	return execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON!, ['-c', script, ...args], {
		cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONPATH: path.join(root, 'backend'), CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE, CORSIA_STATE_DIR: process.env.CORSIA_E2E_STATE_DIR, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
	});
}

test('draft account is used by the first send', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Both viewport sizes are captured in this run');
	const seeded = JSON.parse(fixture(`
import json, os
from pathlib import Path
from connections.account_evidence import mark_discovery
from connections.account_receipts import capture_receipt
from connections import harness_usage
from connections.models import upsert_connection
from connections.vendor_accounts import add_account, default_account, set_account_state
from infra.database import get_db
upsert_connection(type='cli', provider='codex', status='available', config={'binary': '/bin/e2e-codex', 'app_server': True})
upsert_connection(type='cli', provider='claude-code', status='available', config={'binary': '/bin/e2e-claude'})
db = get_db()
now = '2026-10-28T00:00:00+00:00'
model = 'codex/e2e-draft-account'
db.execute("""INSERT OR REPLACE INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,enabled,available,context_window,supports_vision,supports_tools,supports_reasoning,default_max_tokens,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'e2e',1,1,128000,0,1,0,8192,?,?)""", (model,'codex','codex','e2e-draft-account',model,'E2E Draft Account','codex',now,now))
a = default_account('codex', db=db)
b = add_account('codex', 'Draft Picked Account', db=db)
b = set_account_state(b['id'], 'signed_in', expected_revision=b['revision'], db=db)
home = Path(b['source_home']).resolve()
assert home.is_relative_to(Path(os.environ['CORSIA_STATE_DIR']).resolve())
(home / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'fixture': 'synthetic'}}))
for account in (a, b):
    receipt = capture_receipt('codex', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={model: {'available': True, 'context_window': 128000}}, db=db)
db.commit()
harness_usage.record_allowed(provider='codex', receipt=capture_receipt('codex', account_id=b['id'], db=db), source='discovery', db=db)
other_model = 'claude-code/e2e-draft-account'
db.execute("""INSERT OR REPLACE INTO models (id,harness,provider,provider_model_id,litellm_model,display_name,backend,source,enabled,available,context_window,supports_vision,supports_tools,supports_reasoning,default_max_tokens,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'e2e',1,1,128000,0,1,0,8192,?,?)""", (other_model,'claude-code','claude-code','e2e-draft-account',other_model,'E2E Other Vendor','claude-code',now,now))
first = default_account('claude-code', db=db)
other = add_account('claude-code', 'Other Vendor Account', db=db)
other = set_account_state(other['id'], 'signed_in', expected_revision=other['revision'], db=db)
for account in (first, other):
    receipt = capture_receipt('claude-code', account_id=account['id'], db=db)
    assert mark_discovery(account['id'], receipt=receipt, status='ready', models={other_model: {'available': True}}, db=db)
db.commit()
print(json.dumps({'chosen': b['id'], 'other': other['id'], 'default': a['id']}))
db.close()
`).trim()) as { chosen: string; other: string; default: string };
	const { chosen, other } = seeded;
	await page.setViewportSize({ width: 1440, height: 900 });
	await openAppByUrl(page);
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
	await page.getByText('E2E Draft Account', { exact: false }).first().click();
	fixture(`
import sys
from connections import harness_usage
from connections.account_receipts import capture_receipt
from infra.database import get_db
db = get_db()
harness_usage.record_limited(provider='codex', receipt=capture_receipt('codex', account_id=sys.argv[1], db=db), reason='usage_limit', resets_at='2099-01-01T00:00:00+00:00', source='rate_limit_update', db=db)
db.close()
`, seeded.default);
	await page.locator('button.model-chip').click();
	await page.locator('button.model-chip').click();
	await expect(page.locator('.limit-held')).toBeVisible();
	const otherChooser = page.locator('.model-sheet button.picker-account-title').filter({ hasText: 'Claude Code' });
	await expect(otherChooser).toBeVisible();
	await otherChooser.click();
	const otherRow = page.locator('button.account-usage-row.picker', { hasText: 'Other Vendor Account' });
	await otherRow.click();
	await expect(otherRow).toHaveAttribute('aria-pressed', 'true');
	const chooser = page.locator('.model-sheet button.picker-account-title').filter({ hasText: 'Codex' });
	await expect(chooser).toBeVisible();
	await chooser.click();
	const row = page.locator('button.account-usage-row.picker', { hasText: 'Draft Picked Account' });
	await expect(row).toBeEnabled();
	await row.click();
	await expect(row).toHaveAttribute('aria-pressed', 'true');
	await expect(page.locator('.limit-held')).toHaveCount(0);
	await expect(page.locator('button.model-chip')).toContainText('E2E Draft Account');
	const shotPath = (name: string) => process.env.CORSIA_E2E_SHOT_DIR
		? path.join(process.env.CORSIA_E2E_SHOT_DIR, name) : testInfo.outputPath(name);
	await page.screenshot({ path: shotPath('draft-1440.png'), animations: 'disabled' });
	await page.setViewportSize({ width: 393, height: 900 });
	await page.screenshot({ path: shotPath('draft-393.png'), animations: 'disabled' });
	await page.locator('button.model-chip').click();
	const response = page.waitForResponse((r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/threads');
	await page.getByRole('textbox', { name: 'Message CORSIA' }).fill('Hello');
	await expect(page.locator('.send-btn')).toBeEnabled();
	await page.getByRole('textbox', { name: 'Message CORSIA' }).press('Enter');
	const created = await response;
	expect(created.ok(), await created.text()).toBe(true);
	expect(created.request().postDataJSON().account_id).toBe(chosen);
	const threadId = (await created.json()).thread_id;
	const actual = fixture(`
import sys
from infra.database import get_db
db = get_db()
row = db.execute('SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?', (sys.argv[1], 'codex')).fetchone()
print(row['account_id'] if row else '')
db.close()
`, threadId).trim();
	expect(actual).toBe(chosen);
	console.log('draft account evidence:', JSON.stringify({ chosen, actual, other }));
});
