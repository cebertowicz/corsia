import { replyField } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect, type BrowserContext } from '@playwright/test';
import {
	createComment,
	deleteRenderedLine,
	openLiveDocument,
	routeLiveSocket,
	seedLiveDocument,
} from '../support/liveDocument';

/**
 * The document, comments store, and placement owner remain real. This only
 * faults the requested placement response at the live transport boundary.
 */
type ClientFrame = { type?: string; artifact_id?: string; store_id?: string; request_id?: string };

async function refuseNextPlacementRequest(context: BrowserContext) {
	let refuse = false;
	let refused = 0;
	await context.routeWebSocket(url => url.pathname === '/ws', socket => {
		const server = socket.connectToServer();
		socket.onMessage(payload => {
			let frame: ClientFrame | null = null;
			try { frame = JSON.parse(String(payload)) as ClientFrame; } catch { /* Forward non-JSON frames unchanged. */ }
			if (refuse && frame?.type === 'doc_positions' && frame.artifact_id && frame.store_id && frame.request_id) {
				refuse = false;
				refused += 1;
				socket.send(JSON.stringify({
					type: 'doc_error', artifact_id: frame.artifact_id, store_id: frame.store_id,
					request_id: frame.request_id, kind: 'refused', message: 'Comment positions were refused',
				}));
				return;
			}
			server.send(payload);
		});
		server.onMessage(payload => socket.send(payload));
	});
	return { arm: () => { refuse = true; }, refused: () => refused };
}

test('@slice1 @surface:live-document a refused placement reload at 393 preserves the draft, selection, orphan recovery, and error state', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'This is the required 393px mobile recovery proof.');
	test.setTimeout(120_000);
	const stamp = `393-${Date.now()}`;
	const removed = `remove this quote ${stamp}`;
	const retained = `keep this quote ${stamp}`;
	const body = `# Placement recovery ${stamp}\n\n${removed}\n\n${retained}\n`;
	const document = await seedLiveDocument(request, `placement-recovery-${stamp}`, body);
	await createComment(request, document, removed, `Remove ${stamp}`);
	await createComment(request, document, retained, `Keep ${stamp}`);
	const writerContext = await browser.newContext({ viewport: { width: 393, height: 852 } });
	const readerContext = await browser.newContext({ viewport: { width: 393, height: 852 } });
	try {
		const refuse = await refuseNextPlacementRequest(readerContext);
		const writer = await writerContext.newPage();
		const reader = await readerContext.newPage();
		await openLiveDocument(writer, document);
		await openLiveDocument(reader, document);
		await expect(reader.locator('.cm-comment-anchor')).toHaveCount(2);
		const editor = reader.locator('.cm-content');
		// Seed the editor's retained selection without clicking the existing comment anchor, which opens the mobile drawer.
		await editor.evaluate((content, marker) => {
			const line = [...content.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => candidate.textContent?.includes(marker));
			if (!line) throw new Error('retained comment line is not rendered');
			const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
			const text = [...Array.from({ length: 20 }, () => walker.nextNode())].find((node): node is Text => node instanceof Text && node.data.includes('keep'));
			if (!text) throw new Error('retained comment line has no selectable keep text node');
			const start = text.data.indexOf('keep');
			const range = document.createRange();
			range.setStart(text, start);
			range.setEnd(text, start + 4);
			const selection = window.getSelection()!;
			selection.removeAllRanges();
			selection.addRange(range);
			(content as HTMLElement).focus();
		}, retained);
		const editorSelection = () => reader.evaluate(() => {
			const selected = window.getSelection();
			return { text: selected?.toString(), anchor: selected?.anchorOffset, head: selected?.focusOffset,
				line: selected?.anchorNode?.parentElement?.closest('.cm-line')?.textContent };
		});
		const selectedRange = await editorSelection();
		expect(selectedRange.text).toBe('keep');
		await reader.getByRole('button', { name: /^2 open threads\b/ }).click();
		const comments = reader.getByRole('region', { name: 'Document comments', exact: true });
		await expect(comments).toBeVisible();
		const retainedCard = comments.locator('[data-comment-id]').filter({ hasText: `Keep ${stamp}` });
		const retainedId = await retainedCard.getAttribute('data-comment-id');
		expect(retainedId).toBeTruthy();
		const openComments = async () => {
			await reader.getByRole('button', { name: /open thread/ }).click();
			await expect(comments).toBeVisible();
		};
		const closeComments = async () => {
			await comments.getByRole('button', { name: 'Close comments', exact: true }).click();
			await expect(comments).toHaveCount(0);
		};
		let reply = await replyField(comments.locator(`[data-comment-id="${retainedId}"]`));
		await reply.fill(`draft ${stamp}`);
		await expect(reply).toBeFocused();
		await reply.evaluate(element => {
			const input = element as HTMLInputElement;
			input.setSelectionRange(2, 5);
			input.dispatchEvent(new Event('select', { bubbles: true }));
		});
		const replySelection = () => reply.evaluate(element => {
			const input = element as HTMLInputElement;
			return [input.selectionStart, input.selectionEnd];
		});
		expect(await replySelection()).toEqual([2, 5]);

		await deleteRenderedLine(writer, removed);
		await expect(reader.locator('.cm-content')).not.toContainText(removed);
		await expect(reader.locator('.comment-status:has(.unplaced-dot)')).toBeVisible();
		const reload = comments.getByRole('button', { name: 'Reload comment placements', exact: true });
		await expect(reload).toBeVisible();
		await reply.focus();

		// A real resolver answer with no range keeps orphan recovery available and announces the count.
		await reload.click();
		await expect(comments.getByRole('status').filter({ hasText: "1 comment still couldn't be placed" })).toBeVisible();
		await expect(reload).toBeFocused();
		await expect(reload).not.toHaveAttribute('aria-disabled', 'true');
		await closeComments();
		await openComments();
		reply = await replyField(comments.locator(`[data-comment-id="${retainedId}"]`));
		await expect(reply).toHaveValue(`draft ${stamp}`);
		expect(await reader.evaluate(() => document.activeElement?.getAttribute('placeholder'))).not.toBe('Reply…');
		await reply.focus();
		expect(await replySelection()).toEqual([2, 5]);
		// Refocusing observes the editor's retained range; input focus owns the DOM selection meanwhile.
		await editor.focus();
		await expect.poll(editorSelection).toEqual(selectedRange);

		refuse.arm();
		await reload.click();
		await expect.poll(refuse.refused, { timeout: 20_000 }).toBe(1);
		await expect(comments.getByRole('status').filter({ hasText: "Couldn't reload comment placements" })).toBeVisible();
		await expect(reload).toBeFocused();
		await expect(reload).not.toHaveAttribute('aria-disabled', 'true');
		await closeComments();
		await openComments();
		reply = await replyField(comments.locator(`[data-comment-id="${retainedId}"]`));
		await expect(reply).toHaveValue(`draft ${stamp}`);
		await reply.focus();
		expect(await replySelection()).toEqual([2, 5]);
		// Refocusing observes the editor's retained range; input focus owns the DOM selection meanwhile.
		await editor.focus();
		await expect.poll(editorSelection).toEqual(selectedRange);
		await reader.screenshot({ path: testInfo.outputPath('comment-placement-refused-393.png') });
	} finally {
		await writerContext.close();
		await readerContext.close();
	}
});

test('@slice1 @surface:live-document a disconnected 393px placement reload is described by the live connection line', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'This is the required 393px mobile recovery proof.');
	test.setTimeout(120_000);
	const stamp = `393-disconnected-${Date.now()}`;
	const quote = `disconnect quote ${stamp}`;
	const document = await seedLiveDocument(request, `placement-disconnected-${stamp}`, `# Disconnect ${stamp}\n\n${quote}\n`);
	await createComment(request, document, quote, `Comment ${stamp}`);
	const context = await browser.newContext({ viewport: { width: 393, height: 852 } });
	try {
		const link = await routeLiveSocket(context);
		const page = await context.newPage();
		await openLiveDocument(page, document);
		await deleteRenderedLine(page, quote);
		await page.locator('.comment-status').click();
		const comments = page.getByRole('region', { name: 'Document comments', exact: true });
		await expect(comments).toBeVisible();
		const reload = comments.getByRole('button', { name: 'Reload comment placements', exact: true });
		await expect(reload).toBeVisible();
		await link.cut();
		await expect(reload).toHaveAttribute('aria-disabled', 'true');
		const describedBy = await reload.getAttribute('aria-describedby');
		expect(describedBy).toBeTruthy();
		await expect(page.locator(`#${describedBy}`)).toHaveText(/Disconnected/);
	} finally {
		await context.close();
	}
});
