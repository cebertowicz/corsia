import { expect } from '@playwright/test';
import { test } from '../support/test';
import { inEditorView } from '../support/liveDocument';
import { enterDocumentEdit } from '../support/documentReading';
import { openReadingDocument, READING_DOC, selectReadingPassage, selectReadingTab } from '../support/phoneReading';

// Chromium scripted selection and keyboard stand-ins only; native phone menus/finger handles remain device checks.
for (const width of [320, 393, 768, 1440]) {
	for (const theme of ['light', 'dark'] as const) {
		test.describe(`@flow:doc-phone-reading @surface:documents ${width} ${theme}`, () => {
			test.beforeEach(({}, info) => test.skip(info.project.name !== (width === 1440 ? 'chromium-desktop' : 'chromium-mobile'), 'matching primary input'));
			test('opening input, protected typing, explicit Edit/Done and actual tab return', async ({ page }, info) => {
				const seeded = await openReadingDocument(page, width, theme);
				const touch = width !== 1440;
				const content = page.locator('.doc .cm-content:not(.cm-md-cell-editor .cm-content)').first();
				await expect(content).toHaveAttribute('contenteditable', touch ? 'false' : 'true');
				if (!touch) { await expect(page.locator('.p-edit, .bar-done')).toHaveCount(0); return; }
				await expect(content).toHaveAttribute('tabindex', '0');
				await selectReadingPassage(page, 'Opening passage');
				await content.focus();
				await page.keyboard.press('Backspace');
				await page.keyboard.type('blocked');
				expect(await inEditorView(page, view => view.state.doc.toString(), null)).toBe(READING_DOC);
				await page.setViewportSize({ width: width + 30, height: 851 });
				await expect(content).toHaveAttribute('contenteditable', 'false');
				await page.setViewportSize({ width, height: 851 });
				await enterDocumentEdit(page);
				await expect(content).toHaveAttribute('contenteditable', 'true');
				expect(await inEditorView(page, view => view.state.selection.main.empty, null)).toBe(true);
				await page.screenshot({ path: info.outputPath('edit.png') });
				// A known text paragraph, never the box centre: that can land on the image widget and open its preview.
				await content.locator('.cm-line').filter({ hasText: 'Passage 2:' }).click();
				await page.keyboard.type('local writing ');
				const edited = await inEditorView(page, view => view.state.doc.toString(), null);
				await selectReadingTab(page, seeded.tabs[1].title, true);
				await selectReadingTab(page, seeded.tabs[0].title, true);
				await expect(content).toHaveAttribute('contenteditable', 'false');
				expect(await inEditorView(page, view => view.state.doc.toString(), null)).toBe(edited);
				await enterDocumentEdit(page);
				await page.getByRole('button', { name: 'Done', exact: true }).click();
				await expect(content).toHaveAttribute('contenteditable', 'false');
				await page.screenshot({ path: info.outputPath('reading-return.png') });
			});
			test('selected passage offers a tappable Comment in Reading and Edit', async ({ page }, info) => {
				await openReadingDocument(page, width, theme);
				if (width === 1440) {
					await selectReadingPassage(page, 'Opening passage');
					await expect(page.locator('.sel-comment')).toHaveCount(1);
					return;
				}
				for (const editing of [false, true]) {
					if (editing) await enterDocumentEdit(page);
					await selectReadingPassage(page, 'Final passage and selection endpoints.');
					const offer = page.locator('.sel-comment');
					await expect(offer).toHaveCount(1);
					await expect(offer).toBeVisible();
					// The button is measured once its width is known (the frame after it appears) and its pop-in has ended.
					const viewport = page.viewportSize()!;
					await expect.poll(async () => {
						const box = await offer.boundingBox();
						return !!box && box.x >= 0 && box.y >= 0 && box.x + box.width <= viewport.width && box.y + box.height <= viewport.height;
					}, { message: 'Comment sits fully inside the screen' }).toBe(true);
					await page.screenshot({ path: info.outputPath(editing ? 'edit-selection.png' : 'reading-selection.png') });
					// Tapping it opens the comment on exactly that passage, and the document's text is unchanged.
					await offer.click();
					const sheet = page.getByRole('dialog', { name: 'Document comments' });
					await expect(sheet.locator('.composer textarea')).toBeVisible();
					await sheet.getByRole('button', { name: 'Close', exact: true }).click();
					await expect(sheet).toBeHidden();
					expect(await inEditorView(page, view => view.state.doc.toString(), null)).toBe(READING_DOC);
				}
			});
		});
	}
}
