import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { test, expect } from '../support/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { expectCommentsShown, openComments, showAllThreads } from '../support/commentDrawer';

const original = '# Proof\n\nThe original sentence is here.\n';
const guidance = (ownerId: string, workspace: string) => {
	const relative = path.join('.threads', ownerId, 'skills', 'wording-proof', 'evals', 'note.md');
	fs.mkdirSync(path.dirname(path.join(workspace, relative)), { recursive: true });
	execFileSync('git', ['init', '-q', workspace]);
	return relative;
};

test('save after deletion keeps typed text and explains the refusal', async ({ page, request }, info) => {
	const doc = await openSuggestionDocument(page, request, original, [], { placeAt: guidance });
	fs.unlinkSync(doc.docPath);
	await doc.editor.click();
	await page.keyboard.press('ControlOrMeta+End');
	await page.keyboard.type('\nUnsaved writing.');
	await expect(doc.editor).toContainText('Unsaved writing.');
	const status = page.locator('.save-error[role="alert"]');
	await expect(status).toHaveText('Couldn’t save because this document was deleted. Your text stays here until you close it, so copy it to keep it.');
	await expect(status).toBeVisible();
	await page.screenshot({ path: info.outputPath('missing-save.png') });
});

test('a deleted file refusal disables Accept and leaves Reject available', async ({ page, request }, info) => {
	const doc = await openSuggestionDocument(page, request, original, [
		{ quote: 'original sentence', replacement: 'accepted sentence', body: 'Apply this change.' },
	], { placeAt: guidance });
	await openComments(page, /^1 comment\b/);
	await expectCommentsShown(doc.drawer);
	await showAllThreads(doc.drawer);
	fs.unlinkSync(doc.docPath);
	await doc.accept(doc.ids[0]).click();
	await expect(doc.row(doc.ids[0]).getByRole('alert')).toHaveText('This document is deleted, so nothing changed.');
	await expect(doc.accept(doc.ids[0])).toBeDisabled();
	await expect(doc.reject(doc.ids[0])).toBeEnabled();
	await expect(doc.row(doc.ids[0]).getByText("The text this suggestion replaces isn't in the document.")).toHaveCount(0);
	await expect(doc.row(doc.ids[0])).not.toContainText('Text changed');
	await expect(doc.row(doc.ids[0]).getByRole('button', { name: 'Show comment: document deleted' })).toContainText('Document deleted');
	await page.screenshot({ path: info.outputPath('missing-suggestion.png') });
});

test('a stale suggestion disables Accept and leaves Reject available', async ({ page, request }, info) => {
	const doc = await openSuggestionDocument(page, request, original, [
		{ quote: 'original sentence', replacement: 'accepted sentence', body: 'Apply this change.' },
	], { placeAt: guidance });
	await openComments(page, /^1 comment\b/);
	await expectCommentsShown(doc.drawer);
	await showAllThreads(doc.drawer);
	fs.writeFileSync(doc.docPath, '# Proof\n\nThe changed wording is here.\n');
	await doc.accept(doc.ids[0]).click();
	await expect(doc.accept(doc.ids[0])).toBeDisabled();
	await expect(doc.reject(doc.ids[0])).toBeEnabled();
	await expect(doc.row(doc.ids[0]).getByText("The text this suggestion replaces isn't in the document.")).toBeVisible();
	await page.screenshot({ path: info.outputPath('stale-suggestion.png') });
});
