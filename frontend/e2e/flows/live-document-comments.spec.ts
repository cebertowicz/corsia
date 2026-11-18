import { createHash } from 'node:crypto';
import { replyField, expectCommentsShown, openComments } from '../support/commentDrawer';
import { isolatedDb } from '../support/imageRegionComments';
import { TEST_TOKEN } from '../support/corsia';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import {
	createComment,
	deleteRenderedLine,
	editorText,
	openLiveDocument,
	seedLiveDocument,
	typeAtDocumentEnd,
} from '../support/liveDocument';

async function waitForPublication(artifactId: string, text: string): Promise<void> {
	const hash = createHash('sha256').update(text).digest('hex');
	await expect.poll(() => isolatedDb([
		'row = connection.execute("SELECT metadata FROM artifact_identities WHERE id=?", (sys.argv[2],)).fetchone()',
		'print(json.loads(row[0] or "{}").get("last_written_sha256", ""))',
	], artifactId).trim(), { timeout: 30_000, message: 'wait for this exact document snapshot to be published' }).toBe(hash);
}

for (const [width, height, project] of [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const) {
	test(`@slice1 @surface:live-document live comment anchors restore duplicate quotes and preserve a reply draft through a remote edit at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const quote = `duplicate quote ${stamp}`;
		const body = `# Comments ${stamp}\n\n${quote}\n\n${quote}\n\nreply anchor ${stamp}\n`;
		const document = await seedLiveDocument(request, `comments-${stamp}`, body);
		await createComment(request, document, quote, `First ${stamp}`, 0);
		await createComment(request, document, quote, `Second ${stamp}`, 1);
		await createComment(request, document, `reply anchor ${stamp}`, `Reply ${stamp}`);
		const writerContext = await browser.newContext({ viewport: { width, height } });
		const readerContext = await browser.newContext({ viewport: { width, height } });
		try {
			const writer = await writerContext.newPage();
			const reader = await readerContext.newPage();
			await openLiveDocument(writer, document);
			await openLiveDocument(reader, document);
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(3);
			await openComments(reader, /^3 comments\b/);
			const comments = reader.getByRole('region', { name: 'Document comments', exact: true });
			await expectCommentsShown(comments);
			const jump = comments.getByRole('button', { name: `Go to text: ${quote}`, exact: true }).first();
			await jump.click();
			await expect(reader.locator('.cm-comment-focused').filter({ hasText: quote })).toBeInViewport();
			const reply = await replyField(comments.locator('[data-comment-id]').filter({ hasText: `Reply ${stamp}` }));
			await reply.fill(`draft ${stamp}`);
			await expect(reply).toBeFocused();

			await typeAtDocumentEnd(writer, `remote edit ${stamp}`);
			await waitForPublication(document.artifactId, await editorText(writer));
			await expect(reader.locator('.cm-content')).toContainText(`remote edit ${stamp}`);
			await expect(reply).toHaveValue(`draft ${stamp}`);
			await expect(reply).toBeFocused();

			await deleteRenderedLine(writer, quote);
			await waitForPublication(document.artifactId, await editorText(writer));
			await expect(reader.locator('.cm-content')).toContainText(quote);
			await deleteRenderedLine(writer, quote);
			await waitForPublication(document.artifactId, await editorText(writer));
			await expect(reader.locator('.cm-content')).not.toContainText(quote);
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(1);
			// Restore the original prefix/suffix context, which distinguishes the two equal quotes.
			// Keep two line breaks before the reply highlight so the caret never opens its popover.
			await writer.locator('.cm-line').filter({ hasText: `Comments ${stamp}` }).click();
			await writer.keyboard.press('End');
			for (let index = 0; index < 2; index += 1) {
				await writer.keyboard.press('Delete');
				await waitForPublication(document.artifactId, await editorText(writer));
			}
			// Use native text insertion: this Android-emulated project drops synthetic Enter keydowns.
			await writer.keyboard.insertText(`\n\n${quote}\n\n${quote}`);
			const restoredText = await editorText(writer);
			await waitForPublication(document.artifactId, restoredText);
			await expect(writer.locator('.cm-line').filter({ hasText: quote })).toHaveCount(2);
			await expect(reader.locator('.cm-line').filter({ hasText: quote })).toHaveCount(2);
			await expect(reader.locator('.cm-content')).toContainText(`reply anchor ${stamp}`);
			await expect(reader.locator('.cm-content')).toContainText(`remote edit ${stamp}`);
			const stored = JSON.parse(isolatedDb([
				'row = connection.execute("SELECT anchor_prefix, anchor_suffix, body FROM doc_comments WHERE artifact_id=? AND body=?", (sys.argv[2], sys.argv[3])).fetchone()',
				'print(json.dumps(row))',
			], document.artifactId, `Second ${stamp}`)) as [string, string, string];
			const secondOffset = restoredText.lastIndexOf(quote);
			expect(stored[2]).toBe(`Second ${stamp}`);
			expect(restoredText.slice(Math.max(0, secondOffset - stored[0].length), secondOffset)).not.toBe(stored[0]);
			expect(restoredText.slice(secondOffset + quote.length, secondOffset + quote.length + stored[1].length)).toBe(stored[1]);
			const response = await request.get(`/api/artifacts/${document.artifactId}/comments`, { params: { token: TEST_TOKEN } });
			expect(response.ok()).toBeTruthy();
			const liveComments = (await response.json()).comments as Array<{ body: string; orphaned: boolean; from_off: number }>;
			expect(liveComments.find(item => item.body === `First ${stamp}`)).toMatchObject({ orphaned: false, from_off: restoredText.indexOf(quote) });
			expect(liveComments.find(item => item.body === `Second ${stamp}`)).toMatchObject({ orphaned: true });
			// Reload restores a quote only when both stored context sides identify it. The first quote retains both;
			// the second lost its earlier prefix during deletion, so it remains reachable as an orphan rather than guessing.
			await expect(comments.getByRole('button', { name: 'Reload comment placements', exact: true })).toHaveCount(0);
			await expect(reply).toHaveValue(`draft ${stamp}`);
			await reader.reload();
			if (width <= 768) await reader.getByRole('button', { name: 'Toggle sidebar' }).click();
			await reader.getByText(document.title, { exact: true }).first().click();
			await expect(reader.locator('.cm-content')).toContainText(quote);
			await openComments(reader, /^3 comments\b/);
			const restoredLines = reader.locator('.cm-line').filter({ hasText: quote });
			await expect(restoredLines.nth(0).locator('.cm-comment-anchor')).toHaveText(quote);
			await expect(restoredLines.nth(1).locator('.cm-comment-anchor')).toHaveCount(0);
			const first = reader.locator('[data-comment-id]').filter({ hasText: `First ${stamp}` });
			const second = reader.locator('[data-comment-id]').filter({ hasText: `Second ${stamp}` });
			await expect(first).toBeVisible();
			await expect(first.locator('.ct-orphan')).toHaveCount(0);
			await expect(second).toBeVisible();
			await expect(second.locator('[data-comment-jump]')).toHaveAttribute('aria-label', 'Show comment: text unavailable');
			await expect(second.locator('[data-comment-jump]')).toHaveText(`Text changed: “${quote}”`);
		} finally {
			await writerContext.close();
			await readerContext.close();
		}
	});
}
