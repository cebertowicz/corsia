import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import type { Locator, Page, TestInfo } from '@playwright/test';
import { test, expect } from '../support/test';
import { expectContrast, TEXT } from '../support/contrast';
import { openSuggestionDocument, setEvidenceViewport } from '../support/docSuggestions';

const original = '# Proof\n\nThe original sentence is here.\n';
const outside = '# Proof\n\nThe sentence an outside edit wrote.\n';
const STATUS = 'Couldn’t save because this document changed after you started editing. Open Details to choose which text to keep.';
const guidance = (ownerId: string, workspace: string) => {
	const relative = path.join('.threads', ownerId, 'skills', 'wording-proof', 'evals', 'note.md');
	fs.mkdirSync(path.dirname(path.join(workspace, relative)), { recursive: true });
	execFileSync('git', ['init', '-q', workspace]);
	return relative;
};

/** An outside edit after the tab opened, then typing in the tab: the save is refused and the bar offers Details. */
async function refused(page: Page, request: Parameters<typeof openSuggestionDocument>[1], info: TestInfo) {
	await setEvidenceViewport(page, info);
	const doc = await openSuggestionDocument(page, request, original, [], { placeAt: guidance });
	fs.writeFileSync(doc.docPath, outside);
	await doc.editor.click();
	await page.keyboard.press('ControlOrMeta+End');
	await page.keyboard.type('\nTyped after the outside edit.');
	const bar = page.locator('.save-error[role="alert"]');
	await expect(bar).toContainText(STATUS);
	await expect(page.locator('.toast')).toHaveCount(0);
	return { doc, bar, details: bar.getByRole('button', { name: 'Details', exact: true }) };
}
const confirmDialog = (page: Page) => page.getByRole('alertdialog');

test('@flow:editor-stale-choice Use the file’s text asks first, then shows the file’s text and saves nothing', async ({ page, request }, info) => {
	const { doc, bar, details } = await refused(page, request, info);
	await details.click();
	await expect(page.getByText('Your text', { exact: true })).toBeVisible();
	await expect(page.getByText('The file’s text', { exact: true })).toBeVisible();
	await expect(page.getByText('The sentence an outside edit wrote.').first()).toBeVisible();
	await page.screenshot({ path: info.outputPath('stale-details.png') });
	// The quiet labels (Copy, and Close on the popover) are readable in both themes, over their composited background.
	// The phone sheet is frosted glass, so there its labels are measured over the worst backdrop the glass can have.
	const sheet = (page.viewportSize()?.width ?? 0) <= 1024;
	await page.mouse.move(2, 2);
	const quiet: Record<string, { target: Locator; min: number; glass?: boolean }> = {
		copyYours: { target: page.getByRole('button', { name: 'Copy your text', exact: true }), min: TEXT, glass: sheet },
		copyFiles: { target: page.getByRole('button', { name: 'Copy the file’s text', exact: true }), min: TEXT, glass: sheet },
	};
	if (!sheet) quiet.close = { target: page.getByRole('button', { name: 'Close', exact: true }), min: TEXT };
	await expectContrast(page, info, 'stale-choice-details', quiet);
	await expect(page.getByText('The file’s text', { exact: true })).toBeVisible();
	await page.getByRole('button', { name: 'Use the file’s text', exact: true }).click();
	await expect(confirmDialog(page)).toContainText('This replaces your text with the file’s text.');
	await expect(doc.editor).toContainText('Typed after the outside edit.');
	await page.screenshot({ path: info.outputPath('stale-confirm.png') });
	await confirmDialog(page).getByRole('button', { name: 'Use the file’s text', exact: true }).click();
	await expect(doc.editor).toContainText('The sentence an outside edit wrote.');
	await expect(doc.editor).not.toContainText('Typed after the outside edit.');
	await expect(bar).toHaveCount(0);
	await page.waitForTimeout(2500);
	expect(await doc.storedText()).toBe(outside);
});

test('@flow:editor-stale-choice Keep your text saves the typed text over the file after a confirmation', async ({ page, request }, info) => {
	const { doc, bar, details } = await refused(page, request, info);
	await details.click();
	await page.getByRole('button', { name: 'Keep your text', exact: true }).click();
	await expect(confirmDialog(page)).toContainText('This replaces the file’s text with yours.');
	await confirmDialog(page).getByRole('button', { name: 'Keep your text', exact: true }).click();
	await expect(bar).toHaveCount(0);
	await expect.poll(() => doc.storedText()).toContain('Typed after the outside edit.');
	await expect(doc.editor).toContainText('Typed after the outside edit.');
});

test('@flow:editor-stale-choice a file that changed again is refused again, with the typed text kept and Details refreshed', async ({ page, request }, info) => {
	const { doc, bar, details } = await refused(page, request, info);
	await details.click();
	await expect(page.getByText('The file’s text', { exact: true })).toBeVisible();
	const second = '# Proof\n\nThe sentence a second outside edit wrote.\n';
	fs.writeFileSync(doc.docPath, second);
	await page.getByRole('button', { name: 'Keep your text', exact: true }).click();
	await confirmDialog(page).getByRole('button', { name: 'Keep your text', exact: true }).click();
	await expect(page.getByText('This document changed again. Choose which text to keep.')).toBeVisible();
	await expect(page.getByText('The sentence a second outside edit wrote.').first()).toBeVisible();
	await page.screenshot({ path: info.outputPath('stale-changed-again.png') });
	await expect(bar).toContainText(STATUS);
	await expect(doc.editor).toContainText('Typed after the outside edit.');
	expect(await doc.storedText()).toBe(second);
});

test('@flow:editor-stale-choice Details that cannot read the file offers Retry and keeps the typed text', async ({ page, request }, info) => {
	const { doc, details } = await refused(page, request, info);
	await page.route((url) => url.pathname === '/api/files/content', (route) => route.request().method() === 'GET' ? route.abort('internetdisconnected') : route.fallback(), { times: 1 });
	await details.click();
	await expect(page.getByText('Couldn’t load the file’s text. Your text stays here.')).toBeVisible();
	await expect(page.getByRole('button', { name: 'Keep your text', exact: true })).toHaveCount(0);
	await page.screenshot({ path: info.outputPath('stale-fetch-failed.png') });
	await page.getByRole('button', { name: 'Retry', exact: true }).click();
	await expect(page.getByText('The file’s text', { exact: true })).toBeVisible();
	await expect(doc.editor).toContainText('Typed after the outside edit.');
});

for (const choice of ['Use the file’s text', 'Keep your text']) {
	test(`@flow:editor-stale-choice File changed · Reload is hidden while a save is refused and follows its usual rule after "${choice}"`, async ({ page, request }, info) => {
		await setEvidenceViewport(page, info);
		const doc = await openSuggestionDocument(page, request, original, [], { placeAt: guidance });
		fs.writeFileSync(doc.docPath, outside);
		// The person returns to the window after the outside edit: the usual way the document is noticed as changed.
		await page.evaluate(() => window.dispatchEvent(new Event('focus')));
		const reload = page.locator('.stale-control');
		await expect(reload).toBeVisible();
		await doc.editor.click();
		await page.keyboard.press('ControlOrMeta+End');
		await page.keyboard.type('\nTyped after the outside edit.');
		const bar = page.locator('.save-error[role="alert"]');
		await expect(bar).toContainText(STATUS);
		await expect(reload).toHaveCount(0);
		await page.screenshot({ path: info.outputPath('stale-refused-no-reload.png') });
		await bar.getByRole('button', { name: 'Details', exact: true }).click();
		await page.getByRole('button', { name: choice, exact: true }).click();
		await confirmDialog(page).getByRole('button', { name: choice, exact: true }).click();
		await expect(bar).toHaveCount(0);
		// The refusal is over, and the control is back to today's rule: the document is current, so it is not offered.
		await expect(reload).toHaveCount(0);
	});
}

// 1440 is the usual desktop; 1025 is the narrowest width that still opens the card (the phone sheet takes over at 1024), so the narrowest column.
for (const size of [{ width: 1440, height: 900 }, { width: 1025, height: 900 }]) {
	test(`@flow:editor-stale-choice the Details card sits above the bar, inside the document pane, at ${size.width}px`, async ({ page, request }, info) => {
		test.skip(info.project.name === 'chromium-mobile', 'the phone opens a sheet, not a card');
		const { details } = await refused(page, request, info);
		await page.setViewportSize(size);
		await details.click();
		await expect(page.getByRole('dialog', { name: 'Choose which text to keep' })).toBeVisible();
		await expect(page.getByText('The file’s text', { exact: true })).toBeVisible();
		await page.mouse.move(2, 2);
		const gap = 8; // the Popover's own gap to what it clears
		const reading = () => page.evaluate((gap) => {
			const card = document.querySelector('[role="dialog"][aria-label="Choose which text to keep"]')!;
			const box = card.getBoundingClientRect();
			const bar = document.querySelector('.save-error[role="alert"]')!;
			const barBox = bar.getBoundingClientRect();
			const pane = bar.closest('.doc')!.getBoundingClientRect();
			// Every line the bar's own sentence is drawn on: the topmost element at its middle must belong to the bar, not the card.
			const lines: DOMRect[] = [];
			for (const node of bar.childNodes) {
				if (node.nodeType !== Node.TEXT_NODE) continue;
				const range = document.createRange();
				range.selectNodeContents(node);
				lines.push(...range.getClientRects());
			}
			const top = (x: number, y: number) => document.elementFromPoint(x, y);
			return {
				lines: lines.length,
				textUncovered: lines.length > 0 && lines.every((line) => { const hit = top(line.left + line.width / 2, line.top + line.height / 2); return !!hit && bar.contains(hit) && !card.contains(hit); }),
				// The card lands above the bar, a gap clear of its top edge, not merely clear of the text lines.
				aboveBar: box.bottom <= barBox.top - gap + 0.5,
				insidePane: box.left >= pane.left - 0.5 && box.right <= pane.right + 0.5,
				// Just past the pane's edge, level with the card: the card must not be what is drawn there.
				edgeClear: !card.contains(top(pane.right + 4, box.top + box.height / 2)),
			};
		}, gap);
		await expect.poll(reading).toMatchObject({ textUncovered: true, aboveBar: true, insidePane: true, edgeClear: true });
		expect((await reading()).lines).toBeGreaterThan(0);
		await page.screenshot({ path: info.outputPath(`stale-card-above-bar-${size.width}.png`) });
	});
}
