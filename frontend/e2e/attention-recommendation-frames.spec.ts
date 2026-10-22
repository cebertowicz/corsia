import { test } from './support/test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import path from 'node:path';
import { createSession, loadPastFirstRunLanding, openAppByUrl } from './support/corsia';

const SOURCE = {
	thread_id: 'child',
	thread_label: 'Reviewer subagent',
	project_id: 'project-1',
	project_label: 'CORSIA',
	archived: false,
	parent_reviewed: true,
};
const CAPPED_RATIONALE =
	'The recommended choice keeps the change reversible while making the next action obvious, so the person can decide now without creating another follow-up task or losing the context that explains why this option is a useful way forward today.';

function decision(over: Record<string, unknown> = {}) {
	return {
		id: 'decision-1',
		plan_id: 'plan-1',
		node_id: 'node-decision',
		source_node_id: 'node-source',
		thread_id: 'child',
		title: 'Where should decision state live?',
		context: 'P5 needs one canonical home for decision content.',
		tradeoff: 'A table is typed; the identity avoids a second row.',
		recommendation: 'B',
		rationale: 'CORSIA owns the identity, so no twin is required.',
		options: [
			{ id: 'A', label: 'Ship the twin', detail: 'Adds one table.' },
			{ id: 'B', label: 'Reuse the identity', detail: 'No schema change.' },
		],
		other_enabled: true,
		order_key: 0,
		revision: 3,
		raised_at: '2026-08-02T10:00:00+00:00',
		source: SOURCE,
		...over,
	};
}

async function waitForDrawerToSettle(page: Page) {
	const drawer = page.locator('[data-adaptive-drawer]');
	const motion = drawer.locator('.drawer-motion');
	await expect(drawer).toBeVisible();
	await expect(motion).toBeVisible();
	await drawer.evaluate(async (element) => {
		await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		await Promise.all(
			element
				.getAnimations({ subtree: true })
				.map((animation) => animation.finished.catch(() => undefined)),
		);
	});
	await expect(drawer).toHaveCSS('opacity', '1');
	await expect(motion).toHaveCSS('opacity', '1');
	await expect(motion).toHaveCSS('transform', 'none');
}

async function openScenario(page: Page, scenario: string) {
	await page.goto(`/_design?frame=attention-dock&scenario=${scenario}`);
	const bar = page.locator('.dock-bar').first();
	await expect(bar).toBeVisible();
	const chip = page.locator('.decision-chip').first();
	if (!(await chip.isVisible())) await bar.click();
	await expect(chip).toBeVisible();
	await waitForDrawerToSettle(page);
	return chip;
}

async function seedQueue(page: Page, current: unknown) {
	await page.route('**/api/attention/queue', async (route) => {
		await route.fulfill({
			status: 200,
			contentType: 'application/json',
			body: JSON.stringify({ current, count: 1, items: [current] }),
		});
	});
}

async function openDesktopChatDecision(page: Page, request: APIRequestContext, label: string) {
	const title = `${label}-${Date.now()}`;
	await createSession(request, title);
	await loadPastFirstRunLanding(page, () => openAppByUrl(page), { lands: true });
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await expect(page.locator('.thread-item.active').filter({ hasText: title })).toHaveCount(1);
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	const dock = page.locator('[data-attention-dock]');
	await expect(dock).toHaveCount(1);
	await dock.locator('.dock-bar').click();
	const chip = page.locator('.decision-chip');
	await expect(chip).toBeVisible();
	await waitForDrawerToSettle(page);
	return chip;
}

async function capture(page: Page, testInfo: TestInfo, name: string) {
	const directory = process.env.PE_FRAME_DIR ?? testInfo.outputDir;
	await page.screenshot({
		path: path.join(directory, `${name}.png`),
		fullPage: true,
		animations: 'disabled',
	});
}

for (const [label, width, height, colorScheme] of [
	['393-light', 393, 852, 'light'],
	['393-dark', 393, 852, 'dark'],
	['1440-light', 1440, 900, 'light'],
	['1440-dark', 1440, 900, 'dark'],
] as const) {
	test(`@surface:attention recommendation frames (${label})`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Frames set their exact viewport directly.');
		await page.setViewportSize({ width, height });
		await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });

		if (width === 1440) {
			await seedQueue(page, { external_id: 'decision-1', renderer: 'decision', detail: decision() });
			const pickOne = await openDesktopChatDecision(page, request, `recommendation-frame-${label}`);
			await expect(pickOne.getByText('Why', { exact: true })).toBeVisible();
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
			await capture(page, testInfo, `pick-one-recommended-${label}`);

			await page.unroute('**/api/attention/queue');
			await seedQueue(page, { external_id: 'decision-1', renderer: 'decision', detail: decision({
				id: 'decision-any',
				title: 'Add these to your agents?',
				tradeoff: 'Ticked items take a few minutes now; unticked ones can be added later.',
				rationale: 'None of them changes anything else on this Mac.',
				pick: 'any',
				recommendation: ['A', 'B'],
				options: [
					{ id: 'A', label: 'Bring over your MCP servers (3)', detail: 'Copies your config.' },
					{ id: 'B', label: 'Web search', detail: 'Needs a search key.' },
					{ id: 'C', label: 'Daily summary', detail: 'One note each morning.' },
				],
			}) });
			const pickAny = await openDesktopChatDecision(page, request, `recommendation-pick-any-frame-${label}`);
			await expect(pickAny.getByRole('checkbox').nth(0)).toBeChecked();
			await expect(pickAny.getByRole('checkbox').nth(1)).toBeChecked();
			await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
			await capture(page, testInfo, `pick-any-two-pre-ticked-${label}`);
		} else {
			const pickOne = await openScenario(page, 'one');
			await expect(pickOne.getByText('Why', { exact: true })).toBeVisible();
			await capture(page, testInfo, `pick-one-recommended-${label}`);

			const pickAny = await openScenario(page, 'pick-any');
			await expect(pickAny.getByRole('checkbox').nth(0)).toBeChecked();
			await expect(pickAny.getByRole('checkbox').nth(1)).toBeChecked();
			await capture(page, testInfo, `pick-any-two-pre-ticked-${label}`);
		}
	});
}

test('@surface:attention recommendation fallback frame (393 light)', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Frames set their exact viewport directly.');
	await page.setViewportSize({ width: 393, height: 852 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
	const chip = await openScenario(page, 'no-recommendation');
	await expect(chip.locator('dl')).toContainText('Why');
	await capture(page, testInfo, 'no-recommendation-fallback-393-light');
});

test('@surface:attention recommendation cap frame (393 light)', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Frames set their exact viewport directly.');
	expect(CAPPED_RATIONALE).toHaveLength(240);
	await page.setViewportSize({ width: 393, height: 852 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
	const chip = await openScenario(page, 'long-rationale');
	await expect(chip.locator('.option-rationale')).toContainText(CAPPED_RATIONALE);
	await capture(page, testInfo, 'pick-one-recommended-b-rationale-cap-393-light');
});
