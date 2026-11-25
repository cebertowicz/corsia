import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { openComments, showAllThreads } from '../support/commentDrawer';

for (const example of [
	{ name: 'guidance document on Mac', guidance: true, windows: false, redo: 'Meta+Shift+z' },
	{ name: 'guidance document on Windows with Ctrl+Y', guidance: true, windows: true, redo: 'Control+y' },
	{ name: 'guidance document on Windows with Ctrl+Shift+Z', guidance: true, windows: true, redo: 'Control+Shift+z', noRedo: true },
	{ name: 'ordinary document on Mac', guidance: false, windows: false, redo: 'Meta+Shift+z' },
	{ name: 'ordinary document on Windows', guidance: false, windows: true, redo: 'Control+y' },
]) {
	test(`@flow:document-suggestions undo and redo an acceptance in an ${example.name}`, async ({ page, context, request }, info) => {
		test.skip(info.project.name !== 'chromium-desktop');
		test.setTimeout(120_000);
		await context.addInitScript((windows) => {
			Object.defineProperty(navigator, 'platform', { get: () => windows ? 'Win32' : 'MacIntel' });
			Object.defineProperty(navigator, 'userAgent', { get: () => windows ? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36' : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36' });
		}, example.windows);
		const text = '---\nname: suggestion-undo\ndescription: A disposable guide.\n---\n\n# Guide\n\nThe old phrase stays here.\n';
		const doc = await openSuggestionDocument(page, request, text, [{ quote: 'The old phrase stays here.', replacement: 'The new phrase stays here.', body: 'Update phrase.' }], {
			splitWidth: 5000,
			...(example.guidance ? { placeAt: (owner: string, workspace: string) => {
				execFileSync('git', ['init', '-q', workspace]);
				const directory = `suggestion-undo-${owner}`;
				fs.mkdirSync(path.join(workspace, 'skills', directory), { recursive: true });
				return `skills/${directory}/SKILL.md`;
			} } : {}),
		});
		const state = async () => {
			const response = await request.get(`/api/artifacts/${doc.artifactId}/comments`, { params: doc.params });
			expect(response.ok()).toBeTruthy();
			return (await response.json()).comments.find((item: { id: string }) => item.id === doc.ids[0]);
		};
		await openComments(page, /^1 comment\b/);
		await showAllThreads(doc.drawer);
		await doc.accept(doc.ids[0]).click();
		await expect.poll(doc.storedText).toContain('The new phrase stays here.');
		await expect.poll(async () => (await state()).decision).toBe('accepted');
		await expect(doc.editor).toContainText('The new phrase stays here.');
		await expect(doc.editor).toHaveAttribute('contenteditable', 'true');
		await doc.editor.click();
		await expect(doc.editor).toBeFocused();
		await page.keyboard.press(example.windows ? 'Control+z' : 'Meta+z');

		if (!example.guidance) {
			await page.waitForTimeout(1200);
			await expect(doc.editor).toContainText('The new phrase stays here.');
			await expect.poll(doc.storedText).toContain('The new phrase stays here.');
			await expect.poll(async () => (await state()).decision).toBe('accepted');
			return;
		}

		await expect(doc.editor).toContainText('The old phrase stays here.');
		await expect.poll(doc.storedText).toContain('The old phrase stays here.');
		await expect.poll(async () => (await state()).decision).toBeNull();

		await expect(doc.editor).toBeFocused();
		const selection = await doc.editor.evaluate((editor) => {
			const selected = window.getSelection();
			if (!selected?.anchorNode || !selected.focusNode || !editor.contains(selected.anchorNode) || !editor.contains(selected.focusNode)) return null;
			const offset = (node: Node, position: number) => {
				const range = document.createRange();
				range.selectNodeContents(editor);
				range.setEnd(node, position);
				return range.toString().length;
			};
			return { anchor: offset(selected.anchorNode, selected.anchorOffset), head: offset(selected.focusNode, selected.focusOffset) };
		});
		expect(selection).toEqual({ anchor: 0, head: 0 });
		// Let the editor's save debounce settle before the Windows redo probe.
		await page.waitForTimeout(1200);
		await expect(page.locator('.save-ghost.saving')).toHaveCount(0);
		await page.keyboard.press(example.redo);

		if (example.noRedo) {
			await page.waitForTimeout(1200);
			await expect(doc.editor).toContainText('The old phrase stays here.');
			await expect.poll(doc.storedText).toContain('The old phrase stays here.');
			await expect(doc.editor).toBeFocused();
			return;
		}
		await expect(doc.editor).toContainText('The new phrase stays here.');
		await expect.poll(doc.storedText).toContain('The new phrase stays here.');

		await expect.poll(async () => (await state()).decision).toBe('accepted');
	});
}
