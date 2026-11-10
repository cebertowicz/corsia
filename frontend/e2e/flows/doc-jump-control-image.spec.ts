import { expect } from '@playwright/test';
import { test } from '../support/test';
import { chatComposer, createSession, loginByUrl, sendButton, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { DOC, clickDocImage, seedImageDocument, overlay, imageSurface } from '../support/docImageComments';
import { isolatedDb } from '../support/heldComments';
import { hitBox } from '../support/hitBox';

// The image viewer's control is the document's control: one rule, one look. It appears only while an unread pin is
// off-frame, and steps aside the moment that pin is on screen, where the pin's own marker answers for it.
// Both sessions are seeded against one backend that outlives the test, and both rows are opened by their title
// text, so a fixed name lets a later test click an earlier test's row and read a document that is not its own.
const RUN = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
let titleSeq = 0;

for (const width of [393, 1440]) {
	for (const theme of ['light', 'dark'] as const) {
		test(`@surface:doc-jump-control-image ${theme} ${width}px: the viewer's control follows the pin, off-frame then in view`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			test.skip((width === 393) !== testInfo.project.name.endsWith('mobile'), 'Use the matching device project');
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const seeded = await seedImageDocument(page, request, 'image-reply', width === 393 ? null : 420);
			const sessionTitle = `Walkthrough review ${RUN}-${(titleSeq += 1)}`;
			const renamed = await request.patch(`/api/threads/${seeded.sessionId}`, { params: { token: TEST_TOKEN }, data: { title: sessionTitle } });
			expect(renamed.ok(), await renamed.text()).toBeTruthy();
			const writerTitle = `Maya Chen ${RUN}-${(titleSeq += 1)}`;
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
			await writer.getByText(writerTitle, { exact: true }).click();
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
			await page.getByText(sessionTitle, { exact: true }).click();
			await clickDocImage(page);
			await expect(overlay(page)).toBeVisible();
			const viewport = overlay(page).locator('.viewport');
			const pin = overlay(page).locator(`[data-region-tag][data-comment-id="${roots[1]}"]`);
			const control = overlay(page).locator('[data-doc-jump] button');

			// At Fit both pins are on screen, so the viewer keeps the control away: the pin's own marker is the notice.
			await expect(pin).toBeVisible();
			await expect(overlay(page).locator('[data-doc-jump]')).toHaveCount(0);

			// Zoom with the viewer's own keyboard zoom (ImageLightbox's '+' and '='), which belong to its viewport
			// (tabindex 0) and are taken by nobody else while the viewer is open. Proof is the image itself getting wider
			// than its fitted size: the bar's percentage readout is not drawn in a leaf this narrow — the bar clips its own
			// zoom buttons and its readout there, which is a finding for the viewer lane, not something this journey has
			// to depend on.
			const imageWidth = () => overlay(page).locator('.viewport img').first().evaluate((el) => el.getBoundingClientRect().width);
			const fitted = await imageWidth();
			expect(fitted, 'the image is drawn at its fitted size').toBeGreaterThan(1);
			for (let step = 0; step < 12; step += 1) await page.keyboard.press('+');
			await expect.poll(async () => await imageWidth() > fitted * 1.5, { message: 'the viewer zoomed past its own size with the keyboard' }).toBe(true);
			// Two pans that actually move the viewport, each with its own correct answer, so a read that only hears the
			// document's scroller shows up as a failure instead of a coin flip. Between them the leg counts mutations
			// inside the overlay that are not the band's own row: if the control arrives with nothing else changing,
			// the pan is what produced it.
			const offFrame = async () => {
				const frame = (await viewport.boundingBox())!;
				const box = await pin.boundingBox();
				return !box || box.y >= frame.y + frame.height - 1;
			};
			const panTo = (target: 'middle' | 'top') => pin.evaluate((el, want) => {
				const vp = el.closest<HTMLElement>('.viewport') ?? el.closest<HTMLElement>('.image-overlay')?.querySelector<HTMLElement>('.viewport');
				if (!vp) throw new Error('no image viewport pans this pin');
				const before = vp.scrollTop;
				if (want === 'middle') vp.scrollTop = before + (el.getBoundingClientRect().top - vp.getBoundingClientRect().top) - vp.clientHeight / 2;
				else vp.scrollTop = 0;
				return { before, after: vp.scrollTop };
			}, target);

			// Pan so that a pin sits mid-frame: with the unread thread on screen the viewer has nothing to offer.
			const centred = await panTo('middle');
			expect(centred.after, 'the pan moved the image viewport').not.toBe(centred.before);
			await expect(pin).toBeInViewport();
			await expect(overlay(page).locator('[data-doc-jump]')).toHaveCount(0);

			const mutinies = await page.evaluate(() => {
				const host = document.querySelector('.image-overlay')!;
				const store = window as unknown as { __overlayMutations?: { n: number; kinds: string[] } };
				store.__overlayMutations = { n: 0, kinds: [] };
				const byBand = (node: Node | null) => {
					const el = node as HTMLElement | null;
					return !!el?.closest?.('[data-doc-jump]') || !!el?.closest?.('[data-doc-bottom-band]') || Array.from((el as HTMLElement | null)?.addedNodes ?? []).every((added) => (added as HTMLElement).closest?.('[data-doc-jump]') || (added as HTMLElement).closest?.('[data-doc-bottom-band]'));
				};
				new MutationObserver((records) => {
					for (const record of records) {
						if (byBand(record.target)) continue;
						if (record.type === 'attributes') {
							store.__overlayMutations!.n += 1;
							if (store.__overlayMutations!.kinds.length < 4) store.__overlayMutations!.kinds.push(`attr:${(record.target as HTMLElement).tagName}.${record.attributeName ?? ''}`);
							continue;
						}
						const nodes = [...Array.from(record.addedNodes), ...Array.from(record.removedNodes)];
						if (nodes.length && nodes.every((added) => (added as HTMLElement).closest?.('[data-doc-jump]') || (added as HTMLElement).closest?.('[data-doc-bottom-band]'))) continue;
						store.__overlayMutations!.n += 1;
						if (store.__overlayMutations!.kinds.length < 4) store.__overlayMutations!.kinds.push(`${record.type}:${(record.target as HTMLElement).tagName ?? ''}`);
					}
				}).observe(host, { childList: true, subtree: true, attributes: true });
				return true;
			});
			expect(mutinies).toBe(true);

			// Pan back to the top: the unread pin falls below the frame, and that scroll alone must answer the control.
			const panned = await panTo('top');
			expect(panned.after, 'the pan back to the top moved the image viewport').not.toBe(panned.before);
			await expect.poll(offFrame, { message: 'the unread pin is scrolled out of the image frame' }).toBe(true);
			await expect(control).toHaveText(/1 new/);
			const quiet = await page.evaluate(() => (window as unknown as { __overlayMutations?: { n: number; kinds: string[] } }).__overlayMutations);
			expect(quiet?.n, `the control arrived from the pan alone, unrelated overlay mutations: ${JSON.stringify(quiet?.kinds)}`).toBe(0);
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
