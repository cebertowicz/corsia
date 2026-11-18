import { replyField, expectCommentsShown, openComments } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import {
	createComment,
	deleteRenderedLine,
	openLiveDocument,
	seedLiveDocument,
	typeAtDocumentEnd,
} from '../support/liveDocument';

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
			await expect(reader.locator('.cm-content')).toContainText(`remote edit ${stamp}`);
			await expect(reply).toHaveValue(`draft ${stamp}`);
			await expect(reply).toBeFocused();

			await deleteRenderedLine(writer, quote);
			await expect(reader.locator('.cm-content')).toContainText(quote);
			await deleteRenderedLine(writer, quote);
			await expect(reader.locator('.cm-content')).not.toContainText(quote);
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(1);
			// Restore the original prefix/suffix context, which distinguishes the two equal quotes.
			// Keep two line breaks before the reply highlight so the caret never opens its popover.
			await writer.locator('.cm-line').filter({ hasText: `Comments ${stamp}` }).click();
			await writer.keyboard.press('End');
			for (let index = 0; index < 2; index += 1) await writer.keyboard.press('Delete');
			// Use native text insertion: this Android-emulated project drops synthetic Enter keydowns.
			await writer.keyboard.insertText(`\n\n${quote}\n\n${quote}`);
			await expect(writer.locator('.cm-line').filter({ hasText: quote })).toHaveCount(2);
			await expect(reader.locator('.cm-line').filter({ hasText: quote })).toHaveCount(2);
			await expect(reader.locator('.cm-content')).toContainText(`reply anchor ${stamp}`);
			await expect(reader.locator('.cm-content')).toContainText(`remote edit ${stamp}`);
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
			const secondId = await second.getAttribute('data-comment-id');
			expect(secondId).toBeTruthy();
			await expect(reader.locator(`.cm-comment-anchor[data-comment-ids*="${secondId}"]`)).toHaveCount(0);
			await expect(second.locator('[data-comment-jump]')).toHaveAttribute('aria-label', 'Show comment: text unavailable');
			await expect(second.locator('[data-comment-jump]')).toHaveText(`Text changed: “${quote}”`);
		} finally {
			await writerContext.close();
			await readerContext.close();
		}
	});
}
