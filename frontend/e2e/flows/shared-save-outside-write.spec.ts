import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { test, expect } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { open, seed } from '../support/reviewFixtures';

test('@flow:shared-save-outside-write a restore refuses an outside edit made during publication', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	test.setTimeout(120_000);
	const doc = await seed(request, 'shared save race', { sharedAsOf: '2026-01-01' });
	const roots = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN } })).json();
	const workspace = roots.roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	execFileSync('git', ['init', '-q', workspace]);
	const current = `${doc.text}\nCurrent version.\n`;
	const saved = await request.put('/api/files/content', {
		params: { token: TEST_TOKEN }, data: { path: doc.docPath, text: current, thread_id: doc.threadId },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const control = '/api/test/shared-save-race';
	const arm = await request.post(`${control}/arm`, { data: { path: doc.docPath } });
	expect(arm.ok(), await arm.text()).toBeTruthy();
	let released = false;
	try {
		await open(page, doc, 'closed', 'light');
		await page.getByRole('button', { name: 'History', exact: true }).click();
		const rows = page.locator('[data-doc-history-drawer] .vrow');
		await expect.poll(() => rows.count()).toBeGreaterThan(1);
		await rows.last().click();
		const response = page.waitForResponse(r => r.url().includes('/api/files/history/restore') && r.request().method() === 'POST');
		await page.getByRole('button', { name: 'Restore', exact: true }).click();
		await expect.poll(async () => (await (await request.get(`${control}/status`, { params: { path: doc.docPath } })).json()).arrived,
			{ message: 'the save reached publication after its first digest check' }).toBe(true);
		const outside = `${doc.text}\nOutside edit survives.\n`;
		fs.writeFileSync(doc.docPath, outside);
		const release = await request.post(`${control}/release`, { data: { path: doc.docPath } });
		expect(release.ok(), await release.text()).toBeTruthy();
		released = true;
		expect((await response).status()).toBe(409);
		await expect(page.getByText('Couldn’t restore this version because this document has newer changes. Check the current document, then try again.')).toBeVisible();
		expect(fs.readFileSync(doc.docPath, 'utf8')).toBe(outside);
		await expect(page.locator('.cm-content')).toContainText('Outside edit survives.');
	} finally {
		if (!released) await request.post(`${control}/release`, { data: { path: doc.docPath } });
	}
});
