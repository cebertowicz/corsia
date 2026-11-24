import type { APIRequestContext, Page } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { createSession, openAppByUrl } from '../support/corsia';
import { settleMotion, settleRect } from '../support/sheetSettle';
import { expect, test } from '../support/test';
import { operatorNode, withPlanContract } from '../../src/lib/__tests__/fixtures/operatorPlan';
import type { OperatorNode } from '../../src/lib/api/plans';

/**
 * A workflow run's steps on the plan sheet show in the run's order, whatever each step is doing, nested runs
 * included. A general-work run part way through: three steps done, its review run with the brief done, the
 * check waiting on a reviewer and the weigh step not started, then the hand-over. Step titles and ids are the
 * shipped general-work and review-work workflows'. Only the thread's plan read is a fixture; the app is real.
 * With STEPS_OUT set, it writes one frame per variant there (1440 light, 393 dark).
 */

const OUT = process.env.STEPS_OUT;
const NOW = Date.now();
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

function step(id: string, parent: string, depth: number, title: string, workflowStep: string, over: Partial<OperatorNode> = {}): OperatorNode {
	return operatorNode({ id, parent_id: parent, depth, title, workflow_step: workflowStep, workflow_ref: 'general-work', ...over } as Partial<OperatorNode>);
}

function done(id: string, parent: string, depth: number, title: string, workflowStep: string, minutesAgo: number): OperatorNode {
	return step(id, parent, depth, title, workflowStep, { state: 'accepted', updated_at: ago(minutesAgo), last_transition_at: ago(minutesAgo) });
}

const RUN = operatorNode({ id: 'run', title: 'Reply to Dana about Saturday', kind: 'phase', container: true, state: 'active', workflow_ref: 'general-work' } as Partial<OperatorNode>);
const CHECK = step('check', 'review', 2, 'Check the result independently', 'check', {
	state: 'active',
	display: { label_key: 'waiting', tone_key: 'waiting' },
	execution: {
		...operatorNode().execution,
		condition: 'waiting',
		causes: [{ kind: 'waiting', reason_key: 'watch', reason: 'the reviewer', owner_kind: 'thread', owner_id: 'reviewer', ends_at: new Date(NOW + 40 * 60_000).toISOString() }],
	},
} as Partial<OperatorNode>);

/** A step sequenced behind an open one, as the server's full read sends it: blocked on a live prerequisite, shown as a wait. */
function after(id: string, parent: string, depth: number, title: string, workflowStep: string, prerequisite: { id: string; title: string }): OperatorNode {
	return step(id, parent, depth, title, workflowStep, {
		state: 'pending',
		unmet_dependencies: [prerequisite.id],
		display: { label_key: 'waiting', tone_key: 'waiting' },
		status_reason: `After ${prerequisite.title}`,
		execution: {
			...operatorNode().execution,
			condition: 'blocked',
			causes: [{ kind: 'blocked', reason_key: 'dependency', dependency_node_id: prerequisite.id, reason: `Waiting for ${prerequisite.title}`, owner_kind: 'system', owner_id: null }],
		},
	} as Partial<OperatorNode>);
}

const NODES: OperatorNode[] = [
	RUN,
	done('ground', 'run', 1, 'Understand the ask and what is known', 'ground', 50),
	done('premortem', 'run', 1, 'Think through how it could fail', 'premortem', 42),
	done('work', 'run', 1, 'Do the work', 'work', 20),
	step('review', 'run', 1, 'Fresh check before you see it', 'review', { kind: 'phase', container: true, state: 'active', workflow_ref: 'review-work' } as Partial<OperatorNode>),
	done('brief', 'review', 2, 'Brief a fresh reviewer', 'brief', 12),
	CHECK,
	after('weigh', 'review', 2, 'Weigh and address the findings', 'weigh', { id: 'check', title: 'Check the result independently' }),
	after('handover', 'run', 1, 'Hand over the result', 'handover', { id: 'review', title: 'Fresh check before you see it' }),
];

const ORDER = [
	'Reply to Dana about Saturday',
	'Understand the ask and what is known',
	'Think through how it could fail',
	'Do the work',
	'Fresh check before you see it',
	'Brief a fresh reviewer',
	'Check the result independently',
	'Weigh and address the findings',
	'Hand over the result',
];

async function openPlan(page: Page, request: APIRequestContext, width: number, theme: 'light' | 'dark') {
	await page.addInitScript((value) => localStorage.setItem('corsia_theme', value), theme);
	const title = `run-steps-order-${Date.now()}`;
	const threadId = await createSession(request, title);
	const entry = withPlanContract({
		plan: { id: `plan-${threadId}`, owner_kind: 'thread', owner_id: threadId, state: 'active', revision: 1, updated_at: ago(0) },
		owner: { label: 'Conversation', project_id: null, thread_id: threadId },
		progress: { done: 4, total: NODES.length, open: NODES.length - 4, liveness: 'advancing' },
		current_node_ids: ['check'], next_node_ids: [], nodes: NODES,
	} as never);
	await page.route(`**/api/plans/operator/threads/${threadId}**`, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
		contract_version: 2, as_of: ago(0), target: { kind: 'thread', id: threadId, label: 'Conversation' },
		revision_token: `run-steps-${threadId}`, attention: { awaiting_count: 0 }, plans: [entry],
	}) }));
	await openAppByUrl(page);
	if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title).first().click();
	if (width < 768) await page.getByRole('button', { name: /^Open plan —/ }).click();
	else {
		const panel = page.getByRole('button', { name: 'Plan', exact: true });
		const box = await panel.boundingBox();
		if (!box || box.x < 0 || box.x + box.width > width) await page.getByRole('button', { name: 'Toggle panel' }).click();
	}
	const tree = page.locator('.plan-tree').first();
	await expect(tree.getByText('Reply to Dana about Saturday')).toBeVisible();
	for (const name of ['Reply to Dana about Saturday', 'Fresh check before you see it']) {
		const expand = tree.getByRole('button', { name: new RegExp(`^Expand ${name}`) });
		if (await expand.count()) await expand.first().click();
	}
	await expect(tree.getByText('Hand over the result')).toBeVisible();
	return tree;
}

const VARIANTS = [
	{ width: 1440, height: 900, theme: 'light' as const },
	{ width: 393, height: 852, theme: 'dark' as const },
];

for (const variant of VARIANTS) {
	test.describe(`run steps order at ${variant.width} ${variant.theme}`, () => {
		test.use({ viewport: { width: variant.width, height: variant.height }, colorScheme: variant.theme, hasTouch: variant.width < 768, isMobile: variant.width < 768 });
		test('a workflow run shows its steps in step order', async ({ page, request }) => {
			const tree = await openPlan(page, request, variant.width, variant.theme);
			const titles = await tree.locator('.tree-row .row-title').allTextContents();
			expect(titles.map((text) => text.trim()).filter((text) => ORDER.includes(text))).toEqual(ORDER);
			if (OUT) {
				fs.mkdirSync(OUT, { recursive: true });
				await settleRect(page, '.plan-tree');
				await settleMotion(page);
				await page.screenshot({ path: path.join(OUT, `run-steps-order-${variant.width}-${variant.theme}.png`) });
			}
		});
	});
}
