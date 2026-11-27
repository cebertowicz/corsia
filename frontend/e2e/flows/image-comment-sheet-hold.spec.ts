import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { seedImage, settled } from '../support/reviewFixtures';
import { apiComment, isolatedDb, tag, touchDrag } from '../support/imageRegionComments';

test.use({ hasTouch: true });

async function tracePress(page: Page, selector: string) {
	const before = await page.evaluate(() => {
		const panel = document.querySelector('.drawer-panel')!.getBoundingClientRect();
		const pin = document.querySelector('[data-region-tag="1"]:not([inert])')!.getBoundingClientRect();
		return { top: panel.top, pinBottom: pin.bottom, pinAbove: pin.top >= 0 && pin.bottom <= panel.top };
	});
	await page.evaluate(() => {
		(window as any).__sheetTops = [];
		const sample = (n: number) => {
			(window as any).__sheetTops.push(document.querySelector('.drawer-panel')!.getBoundingClientRect().top);
			if (n < 40) requestAnimationFrame(() => sample(n + 1));
		};
		requestAnimationFrame(() => sample(0));
	});
	await page.locator(selector).filter({ visible: true }).first().tap();
	await expect.poll(() => page.evaluate(() => (window as any).__sheetTops.length)).toBe(41);
	const after = await page.evaluate(() => {
		const panel = document.querySelector('.drawer-panel')!.getBoundingClientRect();
		const pin = document.querySelector('[data-region-tag="1"]:not([inert])')!.getBoundingClientRect();
		return { top: panel.top, pinBottom: pin.bottom, trace: (window as any).__sheetTops as number[] };
	});
	expect(after.trace.length).toBeGreaterThan(10);
	for (const top of after.trace) expect(Math.abs(top - before.top)).toBeLessThanOrEqual(1);
	expect(Math.abs(after.top - before.top)).toBeLessThanOrEqual(1);
	if (before.pinAbove) expect(after.pinBottom).toBeLessThanOrEqual(after.top);
}

for (const [width, height] of [[393, 852], [320, 640]] as const) {
	for (const action of ['agree', 'earlier'] as const) {
		test(`image thread ${action} keeps its top at ${width}`, async ({ page, request }) => {
			test.setTimeout(180000);
			await page.setViewportSize({ width, height });
			const doc = await seedImage(request, `Image hold ${width} ${action}`);
			const root = await apiComment(request, doc.artifactId, 'What is this box for?', { x: .2, y: .25, w: .25, h: .25 });
			isolatedDb(["connection.execute(\"UPDATE doc_comments SET author='Reviewer', author_kind='agent', author_thread_id=? WHERE id=?\", (sys.argv[2], sys.argv[3]))"], doc.threadId, root);
			const author = await createSession(request, 'Reviewer South');
			for (let i = 0; i < (action === 'earlier' ? 6 : 1); i++) {
				const result = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: { as_session_id: author, body: `Reply ${i + 1}: the box is the header.`, parent_id: root } });
				expect(result.ok()).toBeTruthy();
			}
			await page.addInitScript((d) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [d.threadId]: { tabs: [{ instanceId: d.threadId, artifactId: d.artifactId, path: d.docPath, title: d.name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: d.threadId, visible: true, activeSurface: 'artifact' } }, splitWidth: null })), doc);
			await loginByUrl(page, { waitForComposer: false });
			await page.evaluate(async (id) => (await import('/src/lib/openArtifactInThread.ts')).showArtifactThreadRequest.set({ threadId: id }), doc.threadId);
			await expect(page.getByRole('img', { name: doc.name })).toBeVisible();
			await tag(page, 1).tap();
			await expect(page.locator('.drawer-panel')).toBeVisible();
			await page.waitForTimeout(1500);
			await settled(page);
			await tracePress(page, action === 'agree' ? 'button[title="Agree"]' : '.drawer-panel .ct-earlier');
			if (action === 'earlier' && width === 393) {
				const panel = page.locator('.drawer-panel');
				const handle = page.locator('.drawer-panel .drawer-grabber');
				const box = await handle.boundingBox();
				expect(box).not.toBeNull();
				const beforeTop = (await panel.boundingBox())!.y;
				await touchDrag(page, { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 }, { x: box!.x + box!.width / 2, y: box!.y - 180 });
				await expect.poll(() => panel.evaluate(el => el.style.getPropertyValue('--drawer-text-cap'))).toBe('');
				await expect.poll(async () => (await panel.boundingBox())!.y).toBeLessThanOrEqual(beforeTop - 100);
			}
		});
	}
}
