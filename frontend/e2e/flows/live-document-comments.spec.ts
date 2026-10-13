import { replyField } from '../support/commentDrawer';
import { expect, test } from '@playwright/test';
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
			await reader.getByRole('button', { name: /^3 open threads\b/ }).click();
			const comments = reader.getByRole('region', { name: 'Document comments', exact: true });
			await expect(comments).toBeVisible();
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
			const reload = reader.getByRole('button', { name: 'Reload comment placements', exact: true });
			await expect(reload).toBeVisible();
			const reloadBox = await reload.boundingBox();
			expect(reloadBox).not.toBeNull();
			// Desktop is the shared secondary sm Button's 28px (as the feedback row's Retry); narrow screens widen its hit area to 44.
			expect(reloadBox!.height).toBeGreaterThanOrEqual(width <= 768 ? 44 : 28);
			expect(reloadBox!.x).toBeGreaterThanOrEqual(0);
			expect(reloadBox!.x + reloadBox!.width).toBeLessThanOrEqual(width);
			await reader.screenshot({ path: testInfo.outputPath(`comment-reload-before-${width}.png`) });
			await reload.click();
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(3);
			await expect(reply).toHaveValue(`draft ${stamp}`);
			await expect(reader.getByRole('button', { name: /^3 open threads\b/ })).toBeFocused();
			await expect(reader.getByRole('status').filter({ hasText: 'Comments placed' })).toBeVisible();
			await expect(reload).toHaveCount(0);
			await reader.screenshot({ path: testInfo.outputPath(`comment-reload-success-${width}.png`) });
			await reader.reload();
			if (width <= 768) await reader.getByRole('button', { name: 'Toggle sidebar' }).click();
			await reader.getByText(document.title, { exact: true }).first().click();
			await expect(reader.locator('.cm-content')).toContainText(quote);
			await reader.getByRole('button', { name: /^3 open threads\b/ }).click();
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(3);
			await expect(reader.getByText(`First ${stamp}`, { exact: true })).toBeVisible();
			await expect(reader.getByText(`Second ${stamp}`, { exact: true })).toBeVisible();
		} finally {
			await writerContext.close();
			await readerContext.close();
		}
	});
}
