import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test, expect } from '../support/test';
import { openAppPastLanding, openSettings, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test('refused update shows its reason and own log in Settings Details', async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'requires an isolated mock backend');
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!state || !workspace || !python) throw new Error('disposable E2E roots and Python are required');
	const operation = 'a'.repeat(32);
	const reason = 'Prepared build is missing';
	const log = 'Update refused: Prepared build is missing';
	// Seed only the in-flight operation and its log: starting a real update would
	// launch the lifecycle shell. The real refusal endpoint settles this operation.
	execFileSync(python, ['-c', `
from pathlib import Path
from automation.self_restart import try_acquire
from automation.update_log import record_update_log
from infra.config import LOGS_DIR
operation = '${operation}'
log_path = LOGS_DIR / ('update-' + operation + '.log')
log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text('${log}\\n')
assert try_acquire(None, None, None, None, 'update', kind='update', operation_id=operation) == operation
assert record_update_log(operation, str(log_path))
`], { cwd: path.resolve(process.cwd(), '../backend'), env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: workspace } });

	const refusal = await request.delete('/api/system/drain', { params: { token: TEST_TOKEN, operation_id: operation, failure_code: 'update_refused', reason, backend_healthy: 'true' } });
	expect(refusal.ok()).toBeTruthy();
	const statusResponse = await request.get('/api/system/update-status', { params: { token: TEST_TOKEN } });
	expect(statusResponse.ok()).toBeTruthy();
	const status = await statusResponse.json();
	expect(status.operation).toMatchObject({ operation_id: operation, status: 'failed', failure_code: 'update_refused', error: reason, log_tail: `${log}\n` });

	await openAppPastLanding(page);
	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	const card = page.locator('[data-settings-anchor="update"]');
	const row = card.locator('.last-update');
	await expect(row.getByText('Last update')).toBeVisible();
	await row.getByText('Details', { exact: true }).click();
	await expect(row.locator('details').getByText(`${reason}.`, { exact: true })).toBeVisible();
	await expect(row.locator('details').getByText(log, { exact: true })).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'refusal-details');
});
