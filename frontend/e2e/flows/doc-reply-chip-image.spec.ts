import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { chatComposer, createSession, loginByUrl, sendButton, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { DOC, seedImageDocument, overlay } from '../support/docImageComments';
import { isolatedDb } from '../support/heldComments';
import { hitBox } from '../support/hitBox';

for (const width of [393, 1440]) {
	for (const theme of ['light', 'dark'] as const) {
		test(`@surface:doc-reply-chip-image ${theme} ${width}px: reply arrives while the image is open`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			test.skip((width === 393) !== testInfo.project.name.endsWith('mobile'), 'Use the matching device project');
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const seeded = await seedImageDocument(page, request, 'image-reply', width === 393 ? null : 420);
			const sessionTitle = 'Walkthrough review';
			const renamed = await request.patch(`/api/threads/${seeded.sessionId}`, { params: { token: TEST_TOKEN }, data: { title: sessionTitle } });
			expect(renamed.ok(), await renamed.text()).toBeTruthy();
			const writerTitle = 'Maya Chen';
			const writerId = await createSession(request, writerTitle);
			const image = '![Mock screen](shots/screen.png)';
			const from = DOC.indexOf(image);
			const roots: string[] = [];
			for (const region of [{ x: 0.1, y: 0.15, w: 0.25, h: 0.2 }, { x: 0.55, y: 0.5, w: 0.25, h: 0.2 }]) {
				const response = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, {
					params: { token: TEST_TOKEN },
					data: { body: 'Please review this part.', anchor_text: image, from_off: from, to_off: from + image.length, region, delivery_policy: 'immediate' },
				});
				expect(response.ok(), await response.text()).toBeTruthy();
				roots.push((await response.json()).comment.id as string);
			}
			isolatedDb(['connection.execute("UPDATE doc_comments SET author=? WHERE artifact_id=? AND author_kind=?", (sys.argv[2], sys.argv[3], "user"))'], 'Jordan Lee', seeded.artifactId);
			const writer = await page.context().newPage();
			await writer.setViewportSize({ width: 1440, height: 900 });
			await loginByUrl(writer);
			await writer.getByText(writerTitle, { exact: true }).first().click();
			await waitForWsConnected(writer);
			async function reply(root: string, body: string) {
				await seedMockResponses(request, [
					{ tool_calls: [{ id: `image-reply-${root}`, name: 'doc_comment_create', arguments: JSON.stringify({ parent_id: root, body }) }], finish_reason: 'tool_calls', match_thread: writerId },
					{ content: 'Reply recorded.', finish_reason: 'stop', match_thread: writerId },
				]);
				// A mobile Playwright context stays touch-enabled even at desktop viewport width.
				// Plain Enter inserts a newline on touch, so use the visible Send action.
				await chatComposer(writer).fill(`Reply to ${root}.`);
				await sendButton(writer).click();
				await expect.poll(async () => {
					const response = await request.get(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN } });
					return JSON.stringify(await response.json()).includes(body);
				}, { timeout: 30_000 }).toBe(true);
			}
			await reply(roots[1], 'The save button stays in place.');
			await loginByUrl(page);
			if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(sessionTitle, { exact: true }).first().click();
			await page.locator('.cm-md-image img').first().click();
			await expect(overlay(page)).toBeVisible();
			const initialChip = overlay(page).locator('[data-doc-reply-chip] button');
			await expect(initialChip).toBeVisible();
			const initialBox = (await initialChip.boundingBox())!;
			// Inspect the other pin first, then close its comments: the unread reply belongs to pin two.
			await overlay(page).locator(`[data-region-tag][data-comment-id="${roots[0]}"]:not([inert])`).click();
			await expect(page.locator(`[data-comment-id="${roots[0]}"]`).last()).toBeVisible();
			if (width === 393) {
				const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
				await expect(backdrop).toBeVisible();
				await expect(overlay(page).locator('[data-doc-reply-chip]')).toHaveCount(0);
				const point = { x: initialBox.x + initialBox.width / 2, y: initialBox.y + initialBox.height / 2 };
				const sheetIntercepts = () => page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-adaptive-drawer]'), point);
				expect(await sheetIntercepts()).toBe(true);
				const layer = await page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null);
				await page.getByRole('button', { name: 'Done', exact: true }).click();
				await expect(backdrop.first()).toBeVisible();
				await expect(overlay(page).locator('[data-doc-reply-chip]')).toHaveCount(0);
				expect(await sheetIntercepts()).toBe(true);
				await expect(backdrop).toHaveCount(0);
				if (layer !== null) await expect.poll(() => page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null)).not.toBe(layer);
			} else await page.keyboard.press('Escape');
			const chip = overlay(page).locator('[data-doc-reply-chip] button');
			await expect(chip).toContainText('1 new reply');
			const target = await chip.evaluate((button) => {
				const face = button.getBoundingClientRect();
				const height = Number.parseFloat(getComputedStyle(button, '::after').height);
				return { x: face.x, y: face.y + (face.height - height) / 2, width: face.width, height };
			});
			expect(target.height).toBeGreaterThanOrEqual(44);
			for (const control of await overlay(page).locator('.bar button').all()) {
				if (!await control.isVisible()) continue;
				const box = await hitBox(control);
				expect(target.x < box.x + box.width && box.x < target.x + target.width && target.y < box.y + box.height && box.y < target.y + target.height, 'reply target clears image controls').toBe(false);
			}
			const frameDir = process.env.REPLY_CHIP_SCREENSHOT_DIR;
			if (frameDir) {
				await page.evaluate(async () => {
					const { coreSettings } = await import('/src/lib/stores/settings.ts');
					coreSettings.update((current) => ({ ...current, features: { ...current?.features, debug_version_overlay: false } }));
				});
				await expect(page.locator('.version-overlay')).toHaveCount(0);
				mkdirSync(frameDir, { recursive: true });
				await page.screenshot({ path: path.join(frameDir, `Q1-image-waiting-${theme}-${width}.png`), scale: 'css', animations: 'disabled' });
			}
			await reply(roots[0], 'The header is ready.');
			await expect(chip).toContainText('2 new replies');
			if (frameDir) await page.screenshot({ path: path.join(frameDir, `Q1-image-arrival-${theme}-${width}.png`), scale: 'css', animations: 'disabled' });
			await chip.click();
			await expect(page.getByText('The header is ready.', { exact: true })).toBeVisible();
			await expect(overlay(page)).toBeVisible();
			if (width === 1440) {
				// The visible pin is excluded even when its popover closes on pointerdown.
				await expect(chip).toHaveAttribute('title', '1 new reply');
				await chip.click();
				await expect(page.getByText('The save button stays in place.', { exact: true })).toBeVisible();
			}
			await writer.close();
		});
	}
}
