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
			test('fixed Comment offer and selected endpoint scroll room in Reading and Edit', async ({ page }, info) => {
				await openReadingDocument(page, width, theme);
				if (width === 1440) {
					await selectReadingPassage(page, 'Opening passage');
					await expect(page.locator('.touch-comment-bar')).toHaveCount(0);
					return;
				}
				for (const editing of [false, true]) {
					if (editing) await enterDocumentEdit(page);
					await selectReadingPassage(page, 'Final passage and selection endpoints.');
					const offer = page.locator('.touch-comment-bar.offered');
					await expect(offer).toBeVisible();
					await expect(page.locator('.sel-comment')).toHaveCount(1);
					const target = await offer.getByRole('button', { name: 'Comment', exact: true }).boundingBox();
					expect(target!.height).toBeGreaterThanOrEqual(44);
					expect(target!.width).toBeGreaterThanOrEqual(44);
					const geometry = await offer.evaluate(el => {
						const bar = el.getBoundingClientRect(), viewport = window.visualViewport!;
						const scroller = document.querySelector<HTMLElement>('.doc .cm-scroller')!;
						return { bottom: bar.bottom, visibleBottom: viewport.offsetTop + viewport.height, reserve: parseFloat(getComputedStyle(scroller.querySelector('.cm-content')!).paddingBottom), height: bar.height };
					});
					expect(Math.abs(geometry.bottom - geometry.visibleBottom)).toBeLessThanOrEqual(1);
					expect(geometry.reserve).toBeGreaterThan(geometry.height);
					await page.screenshot({ path: info.outputPath(editing ? 'edit-selection.png' : 'reading-selection.png') });
					// Tapping it opens the comment on exactly that passage, and the document's text is unchanged.
					await offer.getByRole('button', { name: 'Comment', exact: true }).click();
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

// A selection taller than the screen: its end is scrolled into view and the one Comment offer stays on screen, below the last selected line.
for (const width of [320, 393]) {
	test.describe(`@flow:doc-phone-reading @surface:documents ${width} selection taller than the screen`, () => {
		test.beforeEach(({}, info) => test.skip(info.project.name !== 'chromium-mobile', 'touch'));
		test('Comment stays in the fixed bar and the selected end clears it, in Reading and Edit', async ({ page }, info) => {
			await openReadingDocument(page, width);
			for (const editing of [false, true]) {
				if (editing) await enterDocumentEdit(page);
				await inEditorView(page, view => {
					const text = view.state.doc.toString(), at = text.indexOf('Opening passage'), end = text.length - 1;
					view.dispatch({ selection: { anchor: at, head: end }, effects: view.constructor.scrollIntoView(end, { y: 'end' }) });
				}, null);
				const bar = page.locator('.touch-comment-bar.offered');
				await expect(bar).toBeVisible();
				const button = bar.getByRole('button', { name: 'Comment', exact: true });
				await expect(button).toBeVisible();
				await expect.poll(async () => {
					const box = (await button.boundingBox())!, viewport = page.viewportSize()!;
					return box.height >= 44 && box.width >= 44 && box.y >= 0 && box.y + box.height <= viewport.height;
				}).toBe(true);
				const barTop = (await bar.boundingBox())!.y;
				const endBottom = await inEditorView(page, view => view.coordsAtPos(view.state.doc.length - 1)!.bottom, null);
				expect(endBottom, 'the last selected line is not under the bar').toBeLessThanOrEqual(barTop);
				expect(await inEditorView(page, view => view.state.selection.main.to - view.state.selection.main.from, null), 'the selection is longer than the screen').toBeGreaterThan(1000);
				await page.screenshot({ path: info.outputPath(editing ? 'tall-edit.png' : 'tall-reading.png') });
			}
		});
	});
}

// Edit is the person's choice and lasts until Done: the revision refresh the first save sets off replaces the tab object (same id) and must not end it.
for (const width of [320, 393]) {
	test.describe(`@flow:doc-phone-reading @surface:documents ${width} Edit survives the first save`, () => {
		test.beforeEach(({}, info) => test.skip(info.project.name !== 'chromium-mobile', 'touch opening'));
		test('Edit stays on after the revision refresh that follows the first typed key', async ({ page }) => {
			const seeded = await openReadingDocument(page, width);
			const content = page.locator('.doc .cm-content:not(.cm-md-cell-editor .cm-content)').first();
			const latest = () => page.evaluate((id) => {
				const stored = JSON.parse(localStorage.getItem('corsia:artifactWorkspace:v1') ?? '{}');
				return Object.values<any>(stored.threads ?? {}).flatMap(thread => thread.tabs).find(tab => tab.instanceId === id)?.latestRevision ?? null;
			}, seeded.tabs[0].instanceId);
			await enterDocumentEdit(page);
			await content.locator('.cm-line').filter({ hasText: 'Passage 2:' }).click();
			const before = await latest();
			const refreshed = page.waitForResponse(response => response.url().includes('/api/files/revision'));
			await page.keyboard.type('Q');
			await refreshed;
			// The refresh has landed once the tab carries the new revision; two frames later its effects have run.
			await expect.poll(latest, { message: 'the first save is followed by a revision refresh' }).not.toBe(before);
			await page.evaluate(() => new Promise<void>(done => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
			await expect(content, 'Edit is still on').toHaveAttribute('contenteditable', 'true');
			await expect(page.getByRole('button', { name: 'Done', exact: true })).toBeVisible();
			await page.keyboard.type('W');
			await expect.poll(() => inEditorView(page, view => view.state.doc.toString(), null), { message: 'the second key lands next to the first' }).toContain('QW');
		});
	});
}
