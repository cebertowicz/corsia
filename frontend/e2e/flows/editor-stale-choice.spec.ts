import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import type { Page, TestInfo } from '@playwright/test';
import { test, expect } from '../support/test';
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
