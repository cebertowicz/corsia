import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { test, expect } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { open, seed } from '../support/reviewFixtures';

test('@probe:deleted-restore-dev records a restore after deletion', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const doc = await seed(request, 'deleted restore dev', { sharedAsOf: '2026-01-01' });
	const roots = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN } })).json();
	const workspace = roots.roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	execFileSync('git', ['init', '-q', workspace]);
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: doc.docPath, text: `${doc.text}\nCurrent version.\n`, thread_id: doc.threadId } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const control = '/api/test/shared-save-race';
	expect((await request.post(`${control}/arm`, { data: { path: doc.docPath } })).ok()).toBeTruthy();
	let released = false;
	try {
		await open(page, doc, 'closed', 'light');
		await page.getByRole('button', { name: 'History', exact: true }).click();
		const rows = page.locator('[data-doc-history-drawer] .vrow');
		await expect.poll(() => rows.count()).toBeGreaterThan(1);
		await rows.last().click();
		const response = page.waitForResponse(r => r.url().includes('/api/files/history/restore') && r.request().method() === 'POST');
		await page.getByRole('button', { name: 'Restore', exact: true }).click();
		await expect.poll(async () => (await (await request.get(`${control}/status`, { params: { path: doc.docPath } })).json()).arrived).toBe(true);
		fs.unlinkSync(doc.docPath);
		expect((await request.post(`${control}/release`, { data: { path: doc.docPath } })).ok()).toBeTruthy();
		released = true;
		const result = await response;
		console.log('RESTORE_STATUS', result.status(), 'BODY', await result.text());
		await page.waitForTimeout(1000);
		console.log('RESTORE_SURFACE', await page.locator('[data-doc-history-drawer]').innerText());
		await page.screenshot({ path: info.outputPath('deleted-restore-dev.png') });
	} finally {
		if (!released) await request.post(`${control}/release`, { data: { path: doc.docPath } });
	}
});
