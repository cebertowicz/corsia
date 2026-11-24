import { expect } from '@playwright/test';
import { test } from '../support/test';
import { enterDocumentEdit } from '../support/documentReading';
import { openReadingDocument, selectReadingTab } from '../support/phoneReading';
import { inEditorView } from '../support/liveDocument';
import { settled } from '../support/tableCells';

for (const width of [320, 393, 768]) {
	test.describe(`@flow:doc-phone-reading-selection @surface:documents ${width}`, () => {
		test.beforeEach(({}, info) => test.skip(info.project.name !== 'chromium-mobile', 'primary touch'));
		test('native cell range survives focused pending marks and comment draft mode switches', async ({ page }) => {
			await openReadingDocument(page, width);
			const cell = page.locator('.cm-md-table td').filter({ hasText: /^Cell passage$/ });
			await expect(cell).toBeVisible();
			const selectCell = async () => {
				await page.locator('.doc .cm-content').first().focus();
				// A scripted range set in the same tick as focus() is read back by CodeMirror's DOM observer on its focus flush and collapsed
				// (DOMObserver.flush -> applyDOMChange -> dispatchReadingTransactions); a person's long-press never sets a range that close to a focus.
				// Let the focus settle first. (A real long-press in a table cell selects nothing on dev either: tracked separately.)
				await expect.poll(() => inEditorView(page, view => view.hasFocus, null)).toBe(true);
				await page.evaluate(() => new Promise(requestAnimationFrame));
				await cell.evaluate(el => {
					const range = document.createRange(); range.selectNodeContents(el);
					const selection = document.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
					document.dispatchEvent(new Event('selectionchange'));
				});
			};
			await selectCell();
			await expect(cell.locator('.cm-comment-pending')).toBeVisible();
			expect(await page.evaluate(() => document.getSelection()?.toString())).toBe('Cell passage');
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			await page.locator('.sel-comment').click();
			const sheet = page.getByRole('dialog', { name: 'Document comments' });
			await sheet.locator('.composer textarea').fill('Keep the cell note');
			// The draft sheet is modal and Edit is not offered while drafting: close it as a person would, then use the menu.
			await sheet.getByRole('button', { name: 'Close', exact: true }).click();
			await expect(sheet).toBeHidden();
			await enterDocumentEdit(page);
			await expect(page.locator('.doc .cm-content').first()).toHaveAttribute('contenteditable', 'true');
			await page.getByRole('button', { name: 'Done', exact: true }).click();
			await expect(page.locator('.doc .cm-content').first()).toHaveAttribute('contenteditable', 'false');
			await settled(page);
			await selectCell();
			await expect(cell.locator('.cm-comment-pending')).toBeVisible();
			await page.locator('.sel-comment').click();
			await expect(sheet.locator('.composer textarea'), 'reopening the comment shows the kept draft').toHaveValue('Keep the cell note');
		});
		test('image open/back keeps Edit, while a real tab departure under the image ends it', async ({ page }) => {
			const seeded = await openReadingDocument(page, width);
			await enterDocumentEdit(page);
			await page.locator('.cm-md-image img').first().click();
			await expect(page.locator('.image-overlay')).toBeVisible();
			await page.locator('.image-overlay').getByRole('button', { name: `Back to ${seeded.tabs[0].title}`, exact: true }).click();
			await expect(page.getByRole('button', { name: 'Done', exact: true })).toBeVisible();
			await page.locator('.cm-md-image img').first().click();
			await expect(page.locator('.image-overlay')).toBeVisible();
			await selectReadingTab(page, seeded.tabs[1].title, true);
			await selectReadingTab(page, seeded.tabs[0].title, true);
			if (await page.locator('.image-overlay').isVisible()) await page.locator('.image-overlay').getByRole('button', { name: `Back to ${seeded.tabs[0].title}`, exact: true }).click();
			await expect(page.locator('.doc .cm-content').first()).toHaveAttribute('contenteditable', 'false');
		});
		test('Done preserves current passage and a cell edit, with stale cell keys unable to type', async ({ page }) => {
			await openReadingDocument(page, width);
			await enterDocumentEdit(page);
			await page.locator('.cm-md-table td').filter({ hasText: /^Cell passage$/ }).tap();
			const inner = page.locator('.cm-md-cell-editor .cm-content');
			await expect(inner).toBeFocused();
			await page.keyboard.type(' added');
			const edited = await inEditorView(page, view => view.state.doc.toString(), null);
			await page.getByRole('button', { name: 'Done', exact: true }).click();
			await expect(page.locator('.cm-md-cell-editor')).toHaveCount(0);
			await page.keyboard.press('Backspace');
			expect(await inEditorView(page, view => view.state.doc.toString(), null)).toBe(edited);
		});
	});
}
