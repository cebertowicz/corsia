import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { test } from '../support/test';
import { expect, type TestInfo } from '@playwright/test';
import { createSession } from '../support/corsia';
import {
	batchRows, chatSend, chatSendGroup, heldIn, installWorkspace, isolatedDb, openThread, seedHeldComments, seedTextDocument,
	setDebugVersionOverlay, setMockCorsiaAgentModel, turnOffDocumentComments, watchSendRequests,
} from '../support/heldComments';

// The chat's Send all when the thread's owner cannot take the comments: what a person is told, and that the comments
// stay held on their documents for Retry. The failure is the server's own: the owner's model is no longer addressable,
// the same admission check a real delivery makes. Frames (HELD_SEND_ALL_FAILURE_FRAMES_OUT) are the app's own chat.

const framesOut = process.env.HELD_SEND_ALL_FAILURE_FRAMES_OUT;
const mockOnly = (testInfo: TestInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
};

/** The thread's model no longer resolves, so the server refuses every delivery to its owner. */
function makeOwnerUnaddressable(threadId: string): void {
	isolatedDb(['connection.execute("UPDATE threads SET model=? WHERE id=?", ("mock/model-that-is-gone", sys.argv[2]))'], threadId);
}

const WIDTHS = [1440, 393] as const;
const THEMES = ['light', 'dark'] as const;
const STAGES = ['one-document', 'long-title', 'several-documents', 'request-failed'] as const;

test.describe('@flow:held-send-all-failure @surface:chat Send all names what did not go', () => {
	test.beforeEach(async ({ page, request }) => {
		await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
		await setMockCorsiaAgentModel(request);
		await setDebugVersionOverlay(request, false);
	});

	for (const width of WIDTHS) for (const theme of THEMES) for (const stage of STAGES) {
		test(`${stage}: one toast says what happened and nothing is lost (${width}, ${theme})`, async ({ page, request }, testInfo) => {
			mockOnly(testInfo);
			await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
			await page.emulateMedia({ colorScheme: theme });
			const title = `send-all-failure-${Date.now()}`;
			const threadId = await createSession(request, title);
			const many = stage === 'several-documents';
			const refused = stage === 'request-failed';
			const name = `${width}-${theme}`;
			const first = await seedTextDocument(request, threadId, many ? 'Launch plan' : refused ? 'Meeting notes' : stage === 'long-title' ? 'Quarterly planning and budget review for the growth team' : 'Roadmap', name);
			const second = many ? await seedTextDocument(request, threadId, 'Budget notes', name) : null;
			await seedHeldComments(request, first.artifactId, 2, 'First');
			if (second) await seedHeldComments(request, second.artifactId, 3, 'Second');
			// The canvas is put away, so no document header is on screen and the chat is the one sender.
			await installWorkspace(page, threadId, second ? [first, second] : [first], 0, { visible: false });
			await openThread(page, threadId, title);
			// On a phone the open document fills the screen; the chat is one tap away.
			if (width === 393) await page.getByRole('button', { name: 'Chat', exact: true }).click();
			await expect(chatSend(page)).toHaveText(many ? 'Send all 5 comments' : 'Send 2 comments');
			if (refused) await page.route('**/api/doc-feedback/thread/send', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"Internal error"}' }));
			else makeOwnerUnaddressable(threadId);
			const sends = watchSendRequests(page);
			await chatSend(page).click();

			const text = refused ? 'Couldn’t send your comments — try again'
				: many ? '5 comments couldn’t be delivered. Open each document and choose Retry.'
					: `Your comments on “${first.fileName}” couldn’t be delivered. Open the document and choose Retry.`;
			const toast = page.locator('.toast').filter({ hasText: text });
			await expect(toast).toHaveCount(1);
			await expect(page.locator('.toast')).toHaveCount(1);
			expect(sends.thread, 'one thread send').toHaveLength(1);
			if (refused) {
				// The server never took the send: every comment is still held, and the chat still offers it.
				expect(heldIn(first.artifactId, 'document-held')).toBe(2);
				await expect(chatSend(page)).toHaveText('Send 2 comments');
			} else {
				// Nothing was lost: every comment is kept on its document as undelivered, where its header offers Retry.
				expect(batchRows(first.artifactId)).toEqual([{ policy: 'document-held', state: 'undelivered', items: 2 }]);
				if (second) expect(batchRows(second.artifactId)).toEqual([{ policy: 'document-held', state: 'undelivered', items: 3 }]);
			}
			await expect(page.locator('.version-overlay')).toHaveCount(0);
			if (framesOut) {
				mkdirSync(framesOut, { recursive: true });
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.screenshot({ path: join(framesOut, `${stage}-${width}-${theme}.png`), animations: 'disabled', scale: 'css' });
			}
		});
	}

	for (const width of WIDTHS) for (const theme of THEMES) {
		test(`the held documents are kept on their own headers for Retry after the chat reports the failure (${width}, ${theme})`, async ({ page, request }, testInfo) => {
			mockOnly(testInfo);
			await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
			await page.emulateMedia({ colorScheme: theme });
			const title = `send-all-failure-retry-${Date.now()}`;
			const threadId = await createSession(request, title);
			const doc = await seedTextDocument(request, threadId, title, `retry-${width}-${theme}`);
			await seedHeldComments(request, doc.artifactId, 2);
			// A phone opens the document full screen, so delivery is turned off there, then the chat sends; on desktop the
			// canvas is put away and the document's own toggle is reached after the failure.
			await installWorkspace(page, threadId, [doc], 0, { visible: width === 393 });
			await openThread(page, threadId, title);
			if (width === 393) {
				await turnOffDocumentComments(page);
				await page.getByRole('button', { name: 'Chat', exact: true }).click();
			}
			await expect(chatSend(page)).toHaveText('Send 2 comments');
			makeOwnerUnaddressable(threadId);
			await chatSend(page).click();
			await expect(page.locator('.toast').filter({ hasText: 'couldn’t be delivered' })).toHaveCount(1);
			await expect(chatSendGroup(page)).toHaveCount(0);
			// On a phone the document is one pick from the workspace drawer, away from the chat; on desktop it is the split beside it.
			if (width === 393) {
				await page.getByRole('button', { name: 'Toggle panel' }).click();
				await page.getByRole('button', { name: new RegExp(doc.fileName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) }).click();
			} else {
				await page.getByRole('button', { name: /^Expand artifact panel/ }).click();
				await turnOffDocumentComments(page);
			}
			const retry = page.locator('.undelivered').getByRole('button', { name: /^Retry/ });
			await expect(retry).toBeVisible();
			// The panel slides in on a phone: read the boxes once it has stopped. Each must sit inside the document's own
			// header, not just the window: a reason pushed past the header's left edge is cut off while still on screen.
			const header = page.locator('.document-header');
			const inside = (locator: typeof retry) => async () => {
				const box = await locator.boundingBox();
				const bounds = await header.boundingBox();
				return box !== null && bounds !== null && box.x >= bounds.x - 0.5 && box.x + box.width <= bounds.x + bounds.width + 0.5;
			};
			await expect.poll(inside(retry), { message: 'Retry sits inside the document header' }).toBe(true);
			// The whole reason stays reachable: laid out whole inside the header, or, in a narrow bar, carried by Retry itself.
			const reason = page.locator('.undelivered [role="status"]').filter({ hasText: 'not delivered' });
			await expect(reason).toContainText("its owner can't be reached");
			if (await reason.evaluate((node) => node.classList.contains('sr-only'))) {
				await expect(retry).toHaveAttribute('aria-label', /not delivered: its owner can't be reached/);
			} else {
				await expect.poll(inside(reason), { message: 'the whole reason sits inside the document header' }).toBe(true);
			}
			if (framesOut) {
				// Where the toast sends the person: the document's own header holds the Retry, with the toast long gone.
				mkdirSync(framesOut, { recursive: true });
				await expect(page.locator('.toast')).toHaveCount(0, { timeout: 12000 });
				await page.screenshot({ path: join(framesOut, `retry-on-document-${width}-${theme}.png`), animations: 'disabled', scale: 'css' });
			}
			expect(batchRows(doc.artifactId)).toEqual([{ policy: 'document-held', state: 'undelivered', items: 2 }]);
		});
	}
});
