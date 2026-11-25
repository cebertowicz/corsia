import fs from 'node:fs';
import path from 'node:path';
import { createSession, openAppByUrl } from '../support/corsia';
import { settleMotion } from '../support/sheetSettle';
import { expect, test } from '../support/test';
import { operatorNode, withPlanContract } from '../../src/lib/__tests__/fixtures/operatorPlan';

const out = process.env.INTENT_FRAMES_OUT;
const variants = [
	{ name: 'plain', intent: 'Prepare a useful answer to the request.', acceptance_criteria: 'The answer is ready to use.' },
	{ name: 'workflow', intent: 'Prepare a useful answer to the request.', intent_run_title: 'Prepare the answer', acceptance_criteria: 'The sources have been checked.' },
	{ name: 'worker', intent: 'Prepare a useful answer to the request.', intent_parent_title: 'Prepare the answer', intent_owner_name: 'Research', acceptance_criteria: 'The sources have been checked.' },
	{ name: 'worker-empty', intent: null, intent_parent_title: 'Prepare the answer', intent_owner_name: 'Research', acceptance_criteria: 'The sources have been checked.' },
	{ name: 'long', intent: 'Prepare a clear, useful answer that addresses the whole request and identifies the evidence behind it. Check the sources, preserve the important conditions, and explain any uncertainty in plain language. The result should be ready to use on a phone as well as a desktop.', acceptance_criteria: 'The answer covers the request and its evidence.' },
];

for (const width of [393, 1440]) {
	for (const theme of ['light', 'dark'] as const) {
		test.describe(`${width} ${theme}`, () => {
			test.use({ viewport: { width, height: width === 393 ? 852 : 900 }, colorScheme: theme, hasTouch: width === 393, isMobile: width === 393 });
			test('shows each task reading in details', async ({ page, request }) => {
				await page.addInitScript((value) => localStorage.setItem('corsia_theme', value), theme);
				const title = `task-details-${Date.now()}`;
				const thread = await createSession(request, title);
				const nodes = variants.map((item) => operatorNode({ ...item, id: item.name, title: `Task ${item.name}`, state: 'active' }));
				const entry = withPlanContract({
					plan: { id: `plan-${thread}`, owner_kind: 'thread', owner_id: thread, state: 'active', revision: 1, updated_at: new Date().toISOString() },
					owner: { label: 'Conversation', project_id: null, thread_id: thread },
					progress: { done: 0, total: nodes.length, open: nodes.length, liveness: 'advancing' },
					current_node_ids: [], next_node_ids: [], nodes,
				} as never);
				await page.route(`**/api/plans/operator/threads/${thread}**`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
					contract_version: 2, as_of: new Date().toISOString(), target: { kind: 'thread', id: thread, label: 'Conversation' },
					revision_token: 'test', attention: { awaiting_count: 0 }, plans: [entry],
				}) }));
				await openAppByUrl(page);
				if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
				await page.getByText(title).first().click();
				if (width === 393) await page.getByRole('button', { name: /^Open plan —/ }).click();
				else {
					const panel = page.getByRole('button', { name: 'Plan', exact: true });
					const box = await panel.boundingBox();
					if (!box || box.x < 0 || box.x + box.width > width) await page.getByRole('button', { name: 'Toggle panel' }).click();
				}
				for (const item of variants) {
					const row = page.getByRole('listitem').filter({ hasText: `Task ${item.name}` }).first();
					await row.locator('.row-body').click();
					const details = page.getByRole('region', { name: `Task ${item.name} details` });
					await expect(details).toContainText(item.intent ?? 'No reading recorded.');
					await expect(details).toContainText(item.acceptance_criteria);
					await settleMotion(page);
					if (out) { fs.mkdirSync(out, { recursive: true }); await page.screenshot({ path: path.join(out, `${item.name}-${width}-${theme}.png`) }); }
					await row.locator('.row-body').click();
				}
			});
		});
	}
}
