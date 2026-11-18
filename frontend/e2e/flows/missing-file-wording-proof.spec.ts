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

test('@probe:missing-wording save after deletion retains the typed text', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	const doc = await openSuggestionDocument(page, request, original, [], { placeAt: guidance });
	fs.unlinkSync(doc.docPath);
	await doc.editor.click();
	await page.keyboard.press('ControlOrMeta+End');
	await page.keyboard.type('\nUnsaved writing.');
	await expect(doc.editor).toContainText('Unsaved writing.');
	await expect(page.getByText('This file no longer exists, so your changes weren’t saved. They’re still here; copy them before you close.')).toBeVisible();
	await page.screenshot({ path: info.outputPath('missing-save.png') });
});

test('@probe:missing-wording suggestion after deletion reports the file is gone', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	const doc = await openSuggestionDocument(page, request, original, [
		{ quote: 'original sentence', replacement: 'accepted sentence', body: 'Apply this change.' },
	], { placeAt: guidance });
	await openComments(page, /^1 comment\b/);
	await expectCommentsShown(doc.drawer);
	await showAllThreads(doc.drawer);
	fs.unlinkSync(doc.docPath);
	await doc.accept(doc.ids[0]).click();
	await expect(page.getByText('This file no longer exists, so nothing was changed. You can close it.')).toBeVisible();
	await page.screenshot({ path: info.outputPath('missing-suggestion.png') });
});
