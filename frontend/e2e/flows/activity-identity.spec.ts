import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test, expect } from '../support/test';
import { openAppPastLanding, openSettings, openSettingsSection } from '../support/corsia';

test('a decision draft does not follow a different signed-in person', async ({ page }) => {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state || !workspace || !python) throw new Error('Requires disposable mock roots');
	execFileSync(python, ['-c', "from infra.accounts import create_user; create_user('second-person', 'disposable-test-passphrase', first_run_wake=False)"], {
		cwd: path.resolve(process.cwd(), '../backend'),
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: workspace },
		stdio: 'pipe',
	});
	const source = { thread_id: 'child', thread_label: 'Reviewer', project_id: null, project_label: null, parent_reviewed: true };
	const detail = { id: 'decision-identity', plan_id: 'plan-1', node_id: 'node-1', source_node_id: 'source-1', thread_id: 'child', title: 'Choose a direction', context: 'Choose one.', tradeoff: 'Both are possible.', pick: 'any', recommendation: [], rationale: '', other_enabled: true, options: [{ id: 'A', label: 'First', detail: 'First choice' }], order_key: 0, revision: 1, raised_at: '2026-11-24T10:00:00Z', source };
	const item = { key: 'decision:identity', external_id: detail.id, renderer: 'decision', kind: 'decision', title: detail.title, raised_at: detail.raised_at, source, open: null, rank: null, detail };
	await page.route('**/api/attention/queue**', (route) => route.request().url().includes('view=activity')
		? route.fulfill({ json: { count: 1, items: [item], ranker: null, notifications: { count: 0, items: [] } } })
		: route.fallback());
	await openAppPastLanding(page);
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	const note = page.getByRole('textbox', { name: 'Note' });
	await note.fill('Private draft from the first person');
	await openSettings(page);
	await openSettingsSection(page, 'System');
	await page.getByRole('button', { name: 'Log out' }).click();
	await page.getByRole('button', { name: 'Confirm', exact: true }).click();
	await page.getByPlaceholder('Username').fill('second-person');
	await page.getByPlaceholder('Password').fill('disposable-test-passphrase');
	await page.getByRole('button', { name: /sign in/i }).click();
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	await expect(page.getByRole('textbox', { name: 'Note' })).toHaveValue('');
	await expect(page.getByText('Private draft from the first person')).toHaveCount(0);
});
