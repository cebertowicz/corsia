import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { openThread, pill, seedImage, viewer } from '../support/imageRegionComments';

for (const width of [393, 320, 1440]) test(`image comment task is dismissed from the list at ${width}px`, async ({ page, request }, testInfo) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width, height: 900 });
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!state || !workspace || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const image = await seedImage(page, request, `image-comment-task-${width}`);
	execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', 'feedback', image.artifactId, image.sessionId], {
		cwd: path.resolve(process.cwd(), '..'), encoding: 'utf8',
		env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
	const reviewRead = page.waitForResponse((response) => response.request().method() === 'GET' && new URL(response.url()).pathname === `/api/attention/${image.artifactId}/review`);
	await openThread(page, image.sessionTitle, width);
	const review = await (await reviewRead).json();
	expect(review.person_step?.comment_attention).toBeTruthy();
	await expect(pill(page)).toHaveAccessibleName('4 comments');
	await expect(viewer(page).locator('[data-ask-card]')).toHaveCount(0);
	await page.screenshot({ path: testInfo.outputPath(`image-${width}-rest.png`) });
	await expect(page.getByRole('button', { name: 'Dismiss all', exact: true }), 'no floating Dismiss all over the image').toHaveCount(0);
	await pill(page).click();
	await expect(page.locator('[data-doc-comments-drawer]:visible')).toBeVisible();
	await page.screenshot({ path: testInfo.outputPath(`image-${width}-comments.png`) });
	const drawer = page.locator('[data-doc-comments-drawer]:visible');
	await expect(drawer.getByText(/comments, .*suggestions to review/)).toBeVisible();
	const dismiss = drawer.getByRole('button', { name: 'Dismiss all', exact: true });
	await expect(dismiss).toBeInViewport({ ratio: 1 });
	const geometry = await dismiss.evaluate((button) => {
		const box = button.getBoundingClientRect();
		const heading = button.closest('[data-doc-comments-drawer]')!.querySelector('.drawer-heading')!.getBoundingClientRect();
		return { width: box.width, height: box.height, top: box.top, headingBottom: heading.bottom };
	});
	expect(geometry.width).toBeGreaterThanOrEqual(44);
	expect(geometry.height).toBeGreaterThanOrEqual(44);
	expect(geometry.top).toBeGreaterThanOrEqual(geometry.headingBottom);
	await expect(drawer.locator('.task-who'), 'the asker leads the summary, as it did on its card').toHaveText(new RegExp(`^${image.sessionTitle.slice(0, 8)}`));
	const row = await dismiss.evaluate((button) => {
		const summary = button.closest('.task-dismiss')!.querySelector('.task-summary')!.getBoundingClientRect();
		const box = button.getBoundingClientRect();
		const labelLeft = box.left + Number.parseFloat(getComputedStyle(button).paddingLeft);
		return { sameRow: box.top < summary.bottom, labelLeft, summaryLeft: summary.left };
	});
	// Wrapped onto its own row the label starts where the summary's text starts; beside it, at 393 and wider, it stays on the summary's row.
	if (width === 320) expect(row.sameRow, 'at 320 the button wraps').toBe(false);
	else expect(row.sameRow, 'beside the summary at 393 and wider').toBe(true);
	if (!row.sameRow) expect(Math.abs(row.labelLeft - row.summaryLeft)).toBeLessThanOrEqual(1);
	await dismiss.click({ timeout: 10_000 });
	await expect(pill(page)).toHaveAccessibleName('4 comments');
	await expect.poll(async () => {
		const response = await request.get(`/api/attention/${image.artifactId}/review`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
		expect(response.ok()).toBeTruthy();
		return (await response.json()).person_step?.comment_attention ?? null;
	}).toBeNull();
});
