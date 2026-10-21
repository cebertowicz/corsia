import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

test('@flow:review-failure-status compact failures on phone', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
	await page.setViewportSize({ width: 393, height: 852 });
	const title = `review-status-${Date.now()}`;
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	expect(roots.ok()).toBeTruthy();
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const docPath = path.join(workspace, `${title}.md`);
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: {
		path: docPath, text: '# Review status\n\nA document to review.', thread_id: threadId, create: true,
	} });
	expect(seeded.ok()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const cases = [
		['missing_credentials', 'Couldn’t finish. Sign in again in Settings › Providers.', 'Sign in again in Settings › Providers.'],
		['target_unavailable', 'Couldn’t finish. Choose another model.', 'Choose another model for Quality Gate.'],
		['binary_resolution', 'Couldn’t finish. Ask CORSIA to fix it.', 'Ask CORSIA in chat to fix it.'],
		['provider_error', 'Unfinished. Quality Gate will finish it.', 'Quality Gate will finish it.'],
	];
	// This fixture isolates status rendering; runtime failure delivery has backend coverage.
	await page.route(`**/api/artifacts/${artifactId}/reviews*`, (route) => route.fulfill({ json: {
		reviews: cases.map(([reason], i) => ({ id: `status-${i}`, reviewer_thread_id: `reviewer-${i}`,
			reviewer_name: 'Quality Gate', requester_name: 'Requester', created_at: new Date().toISOString(),
			may_cancel: true, state: 'failed', outcome: 'open', reason })), reviewers: [],
	} }));
	await page.addInitScript(({ threadId, artifactId, docPath, title }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: {
			tabs: [{ instanceId: threadId, artifactId, path: docPath, title, loadedRevision: null,
				latestRevision: null, status: 'loading' }], activeTabId: threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth: 500 }));
	}, { threadId, artifactId, docPath, title });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await page.locator('.reviewer-group').click();
	const rows = page.locator('[data-review-request]');
	await expect(rows).toHaveCount(cases.length);
	for (const [i, [, status, recovery]] of cases.entries()) {
		const line = rows.nth(i).locator('.status');
		await expect(line).toHaveText(status);
		expect(await line.getAttribute('title')).toContain(recovery);
		const measured = await line.evaluate((element) => {
			const range = document.createRange(); range.selectNodeContents(element);
			const style = getComputedStyle(element);
			return { lines: range.getClientRects().length, whiteSpace: style.whiteSpace,
				overflow: style.textOverflow, height: element.getBoundingClientRect().height,
				clientWidth: element.clientWidth, scrollWidth: element.scrollWidth };
		});
		// The status wraps instead of being cut off.
		expect(measured.whiteSpace).not.toBe('nowrap');
		expect(measured.overflow).not.toBe('ellipsis');
		expect(measured.scrollWidth).toBeLessThanOrEqual(measured.clientWidth);
		await info.attach(`status-${i}-geometry`, { body: JSON.stringify(measured), contentType: 'application/json' });
	}
	await saveEvidenceScreenshot(page, info, 'review-failure-status-phone', { fullPage: false });
});
