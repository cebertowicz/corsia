import { expect } from '@playwright/test';
import { test } from '../support/test';
import { chatComposer, createSession, loginByUrl, sendButton, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { DOC, seedImageDocument, overlay, imageSurface } from '../support/docImageComments';
import { isolatedDb } from '../support/heldComments';
import { hitBox } from '../support/hitBox';

// The image viewer's control is the document's control: one rule, one look. It appears only while an unread pin is
// off-frame, and steps aside the moment that pin is on screen, where the pin's own marker answers for it.
for (const width of [393, 1440]) {
	for (const theme of ['light', 'dark'] as const) {
		test(`@surface:doc-jump-control-image ${theme} ${width}px: the viewer's control follows the pin, off-frame then in view`, async ({ page, request }, testInfo) => {
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
			// pin one sits near the top of the image, pin two in the middle: zooming in and scrolling to the top leaves only pin two outside the frame.
			for (const region of [{ x: 0.1, y: 0.15, w: 0.25, h: 0.2 }, { x: 0.55, y: 0.55, w: 0.25, h: 0.2 }]) {
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
			await seedMockResponses(request, [
				{ tool_calls: [{ id: `image-reply-${roots[1]}`, name: 'doc_comment_create', arguments: JSON.stringify({ parent_id: roots[1], body: 'The save button stays in place.' }) }], finish_reason: 'tool_calls', match_thread: writerId },
				{ content: 'Reply recorded.', finish_reason: 'stop', match_thread: writerId },
			]);
			await chatComposer(writer).fill(`Reply to ${roots[1]}.`);
			await sendButton(writer).click();
			await expect.poll(async () => {
				const response = await request.get(`/api/artifacts/${seeded.artifactId}/comments`, { params: { token: TEST_TOKEN } });
				return JSON.stringify(await response.json()).includes('The save button stays in place.');
			}, { timeout: 30_000 }).toBe(true);

			await loginByUrl(page);
			if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(sessionTitle, { exact: true }).first().click();
			await page.locator('.cm-md-image img').first().click();
			await expect(overlay(page)).toBeVisible();
			const viewport = overlay(page).locator('.viewport');
			const pin = overlay(page).locator(`[data-region-tag][data-comment-id="${roots[1]}"]`);
			const control = overlay(page).locator('[data-doc-jump] button');

			// At Fit both pins are on screen, so the viewer keeps the control away: the pin's own marker is the notice.
			await expect(pin).toBeVisible();
			await expect(overlay(page).locator('[data-doc-jump]')).toHaveCount(0);

			// Zoom in and read the top of the image: pin two leaves the frame, and now the viewer says one is waiting.
			for (let step = 0; step < 4; step += 1) await overlay(page).getByRole('button', { name: 'Zoom in' }).click();
			await viewport.evaluate((el) => { el.scrollTop = 0; });
			const offFrame = async () => {
				const frame = (await viewport.boundingBox())!;
				const box = await pin.boundingBox();
				return !box || box.y >= frame.y + frame.height - 1;
			};
			await expect.poll(offFrame, { message: 'the unread pin is scrolled out of the image frame' }).toBe(true);
			await expect(control).toHaveText(/1 new/);
			await expect(control).toHaveAccessibleName(/1 new, next (above|below): /);

			// Its face is the touch target and it stays clear of the viewer's own controls.
			const face = (await control.boundingBox())!;
			expect(face.height, 'the jump control meets its touch target height').toBeGreaterThanOrEqual(width === 393 ? 44 : 32);
			for (const other of await overlay(page).locator('.bar button').all()) {
				if (!await other.isVisible()) continue;
				const box = await hitBox(other);
				expect(face.x < box.x + box.width && box.x < face.x + face.width && face.y < box.y + box.height && box.y < face.y + face.height, 'the jump control clears the image controls').toBe(false);
			}

			// The tap goes to the waiting pin: its answer is shown, and with the pin on screen the control steps aside.
			await control.click();
			await expect(page.getByText('The save button stays in place.', { exact: true })).toBeVisible();
			await expect(overlay(page)).toBeVisible();
			await expect(overlay(page).locator('[data-doc-jump]')).toHaveCount(0);
			await writer.close();
		});
	}
}
