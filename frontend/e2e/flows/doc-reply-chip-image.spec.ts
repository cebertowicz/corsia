import { expect } from '@playwright/test';
import { test } from '../support/test';
import { chatComposer, createSession, loginByUrl, sendButton, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { DOC, seedImageDocument, overlay, expectBarFits, imageSurface } from '../support/docImageComments';
import { drag } from '../support/imageRegionComments';
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
			isolatedDb(['connection.execute("UPDATE doc_comments SET author=? WHERE artifact_id=? AND author_kind=?", (sys.argv[2], sys.argv[3], "user"))', 'connection.execute("UPDATE models SET display_name=? WHERE id=?", ("Claude Sonnet 5.5", "mock-test-model"))'], 'Jordan Lee', seeded.artifactId);
			await injectMessage(request, seeded.sessionId, { role: 'user', content: 'Can you check the header and save button on the walkthrough screen?' }, TEST_TOKEN);
			await injectMessage(request, seeded.sessionId, { role: 'assistant', content: 'I reviewed both and left comments on the image.', metadata: { for_user: true } }, TEST_TOKEN);
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
			await overlay(page).locator('[data-image-comment]').click();
			const imageBox = (await imageSurface(page).boundingBox())!;
			await drag(page, { x: imageBox.x + imageBox.width * 0.1, y: imageBox.y + imageBox.height * 0.7 }, { x: imageBox.x + imageBox.width * 0.3, y: imageBox.y + imageBox.height * 0.9 });
			const imageDraft = page.getByPlaceholder('Comment…');
			await imageDraft.fill('The image draft must survive.');
			await expect(overlay(page).locator('[data-doc-reply-chip]')).toHaveCount(0);
			await expect(imageDraft).toHaveValue('The image draft must survive.');
			await page.getByRole('button', { name: 'Cancel', exact: true }).click();
			await expect(imageDraft).toHaveCount(0);
			await expect(initialChip).toContainText('1 new reply');

			// Inspect the other pin first, then close its comments: the unread reply belongs to pin two.
			await overlay(page).locator(`[data-region-tag][data-comment-id="${roots[0]}"]:not([inert])`).click();
			await expect(page.locator(`[data-comment-id="${roots[0]}"]`).last()).toBeVisible();

			if (width === 393) {
				const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
				await expect(backdrop).toBeVisible();
				await expect(overlay(page).locator('[data-doc-reply-chip]')).toHaveCount(0);
				const point = { x: initialBox.x + initialBox.width / 2, y: initialBox.y + initialBox.height / 2 };
				const chipHit = () => page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-doc-reply-chip]'), point);
				expect(await chipHit()).toBe(false);
				const layer = await page.evaluate(() => (history.state as { backLayer?: number } | null)?.backLayer ?? null);
				await page.getByRole('button', { name: 'Done', exact: true }).click();
				await expect(backdrop.first()).toBeVisible();
				await expect(overlay(page).locator('[data-doc-reply-chip]')).toHaveCount(0);
				expect(await chipHit()).toBe(false);
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
			await overlay(page).locator('.bar').evaluate(async (element) => { await Promise.all(element.getAnimations({ subtree: true }).map((animation) => animation.finished.catch(() => undefined))); });
			// At the narrow desktop canvas, Fit already clips in the base lightbox; preserve that finding separately.
			if (width === 393) await expectBarFits(page);

			await reply(roots[0], 'The header is ready.');
			await expect(chip).toContainText('2 new replies');

			await page.evaluate(() => {
				const samples: unknown[] = [];
				const initial = document.querySelector('.image-overlay [data-doc-reply-chip]');
				(window as unknown as { __chipSamples: unknown[] }).__chipSamples = samples;
				const sample = () => {
					const chips = [...document.querySelectorAll<HTMLElement>('.image-overlay [data-doc-reply-chip]')];
					const uncovered = [...document.querySelectorAll<HTMLElement>('[data-doc-reply-chip]')].filter(node => {
						const box = node.getBoundingClientRect();
						const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
						return !!hit && node.contains(hit);
					}).length;
					samples.push({ same: chips[0] === initial, uncovered, chips: chips.map(node => ({ text: node.innerText, animations: node.getAnimations().length })) });
					if (samples.length < 120) requestAnimationFrame(sample);
				};
				requestAnimationFrame(sample);
			});
			await chip.click();
			await expect(page.getByText('The header is ready.', { exact: true })).toBeVisible();
			await expect(overlay(page)).toBeVisible();
			if (width === 1440) {
				await page.waitForTimeout(300);
				const samples = await page.evaluate(() => (window as unknown as { __chipSamples: Array<{ same: boolean; uncovered: number; chips: Array<{ text: string; animations: number }> }> }).__chipSamples);
				expect(samples.some(sample => sample.chips[0]?.text.includes('1 new reply'))).toBe(true);
				expect(samples.every(sample => sample.same && sample.uncovered === 1 && sample.chips.length === 1 && sample.chips[0].animations === 0)).toBe(true);
				// The visible pin is excluded even when its popover closes on pointerdown.
				await expect(chip).toHaveAttribute('title', '1 new reply');
				await chip.click();
				await expect(page.getByText('The save button stays in place.', { exact: true })).toBeVisible();
			}
			await writer.close();
		});
	}
}
