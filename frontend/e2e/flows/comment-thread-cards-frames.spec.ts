import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/imageRegionComments';
import { syntheticPng } from '../support/syntheticImages';
import { comment, open, seed, settled, type Case } from '../support/reviewFixtures';

const cases: readonly Case[] = [
	['1440 light', 'closed', 'light'], ['1440 dark', 'closed', 'dark'],
	['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark'],
];

async function checkCards(page: Page) {
	const result = await page.locator('[data-doc-comments-drawer]:visible').last().evaluate((drawer) => {
		const cards = [...drawer.querySelectorAll<HTMLElement>('[data-comment-id] > .ct')];
		const failures: string[] = [];
		const rects = cards.map((card) => card.getBoundingClientRect());
		for (let i = 0; i < cards.length; i++) {
			const card = cards[i], box = rects[i];
			if (box.width === 0 || box.height === 0) continue;
			if (box.left < -1 || box.right > window.innerWidth + 1) failures.push(`card ${i} clipped horizontally`);
			for (const child of card.querySelectorAll<HTMLElement>('[data-comment-jump], .ct-head, .ct-markdown-reveal, .ct-compose, .ct-replies, .ct-res-body')) {
				const part = child.getBoundingClientRect();
				if (!part.width || !part.height) continue;
				if (part.left < box.left - 1 || part.right > box.right + 1 || part.top < box.top - 1 || part.bottom > box.bottom + 1) failures.push(`card ${i}: ${child.className} outside`);
			}
			if (i && box.top >= rects[i - 1].top && box.top - rects[i - 1].bottom < 8) failures.push(`card ${i}: gap below 8px`);
		}
		return { count: cards.length, failures };
	});
	expect(result.count).toBeGreaterThan(0);
	expect(result.failures).toEqual([]);
}

for (const [label, mode, theme] of cases) {
	test.describe(label, () => {
		if (mode === 'phone') test.use({ hasTouch: true, isMobile: true });
		test('comment cards at rest, focused and replying', async ({ page, request, browser }) => {
			test.setTimeout(90_000);
			const doc = await seed(request, `Comment cards ${label}`);
			const imageName = `cards-${doc.threadId}.png`;
			fs.writeFileSync(path.join(path.dirname(doc.docPath), imageName), syntheticPng(640, 400));
			const imageAnchor = `![Rollout diagram](${imageName})`;
			doc.text += `\n${imageAnchor}\n`;
			const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: doc.docPath, text: doc.text, thread_id: doc.threadId } });
			expect(saved.ok(), await saved.text()).toBeTruthy();
			const ids = [];
			for (let i = 1; i <= 6; i++) ids.push(await comment(request, doc, `Thread ${i}: Please review this part of the rollout.`, i));
			const from = doc.text.indexOf(imageAnchor);
			const imageComment = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'The diagram needs a label.', anchor_text: imageAnchor, from_off: from, to_off: from + imageAnchor.length, region: { x: 0.2, y: 0.2, w: 0.3, h: 0.3 }, delivery_policy: 'immediate' } });
			expect(imageComment.ok(), await imageComment.text()).toBeTruthy();
			const imageId = (await imageComment.json()).comment.id as string;
			for (const id of ids.slice(0, 2)) {
				for (let i = 1; i <= 5; i++) {
					const response = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body: `Reply ${i}: I have reviewed this and will follow up.`, parent_id: id, delivery_policy: 'immediate' } });
					expect(response.ok()).toBeTruthy();
				}
			}
			isolatedDb([
				'connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[2], sys.argv[3]))',
				'connection.execute("UPDATE doc_comments SET replacement=? WHERE id=?", ("Milestone", sys.argv[4]))',
				'connection.execute("UPDATE doc_comments SET anchor_text=? WHERE id=?", ("Text no longer present", sys.argv[5]))',
				'connection.execute("UPDATE doc_comments SET resolved=1 WHERE id=?", (sys.argv[6],))',
				'connection.execute("UPDATE doc_comments SET acked=1 WHERE id=?", (sys.argv[7],))',
			], doc.threadId, ids[1], ids[3], ids[4], ids[5], ids[2]);
			const baseUrl = process.env.CORSIA_CARDS_BASE_URL;
			if (!baseUrl) throw new Error('CORSIA_CARDS_BASE_URL must name the isolated base frontend');
			const baseline = await browser.newContext({ baseURL: baseUrl, hasTouch: mode === 'phone', isMobile: mode === 'phone' });
			const before = await baseline.newPage();
			await open(before, doc, mode, theme);
			await before.locator('.comment-status').click();
			await expect(before.locator('[data-doc-comments-drawer]')).toBeVisible();
			await before.locator(`[data-comment-id="${ids[0]}"] .ct-earlier`).click();
			await settled(before);
			const dir = process.env.CORSIA_CARDS_FRAMES_DIR;
			if (!dir) throw new Error('CORSIA_CARDS_FRAMES_DIR must name the frame output directory');
			fs.mkdirSync(dir, { recursive: true });
			await before.screenshot({ path: path.join(dir, `before-${mode === 'phone' ? '393' : '1440'}-${theme}.png`), animations: 'disabled' });
			await baseline.close();
			await open(page, doc, mode, theme);
			await page.locator('.comment-status').click();
			const drawer = page.locator('[data-doc-comments-drawer]');
			await expect(drawer).toBeVisible();
			await settled(page);
			await drawer.locator(`[data-comment-id="${ids[0]}"] .ct-earlier`).click();
			await expect(drawer.locator(`[data-comment-id="${ids[0]}"] .ct-earlier`)).toHaveCount(0);
			await expect(drawer.locator(`[data-comment-id="${ids[1]}"] .ct-earlier`)).toBeVisible();
			const capture = async (name: string) => {
				await settled(page);
				await checkCards(page);
				await page.screenshot({ path: path.join(dir, `${name}-${mode === 'phone' ? '393' : '1440'}-${theme}.png`), animations: 'disabled' });
			};
			await capture('after');
			const first = drawer.locator(`[data-comment-id="${ids[0]}"]`);
			await first.locator('.ct').click();
			await expect(first.locator('.ct')).toHaveClass(/focused/);
			await capture('after-focused');
			await first.locator('.ct-input').fill('I will follow up with the team.');
			await capture('after-reply');
			if (mode === 'closed' && theme === 'light') {
				await drawer.locator('[data-drawer-filter]').click();
				await drawer.getByRole('menu', { name: 'Filter comments' }).getByRole('menuitemradio', { name: 'All', exact: true }).click();
				const resolved = drawer.locator(`[data-comment-id="${ids[5]}"]`);
				await resolved.locator('.ct-res-head').click();
				await settled(page);
				const imageRow = drawer.locator(`[data-comment-id="${imageId}"]:visible`);
				await imageRow.scrollIntoViewIfNeeded();
				await capture('after-states');
				// Linked hover is owned by the image viewer, not the document drawer.
				await drawer.getByRole('button', { name: 'Close comments' }).click();
				await page.locator('.cm-md-image img').first().click();
				const viewer = page.locator('.image-overlay');
				await expect(viewer).toBeVisible();
				await viewer.locator('[data-image-comments-pill]').click();
				await settled(page);
				const viewerRow = viewer.locator(`.list > div[data-comment-id="${imageId}"]:visible`).last();
				await viewerRow.hover();
				await expect(viewerRow.locator('.ct')).toHaveClass(/linked/);
				await capture('after-linked');
			}
		});
	});
}
