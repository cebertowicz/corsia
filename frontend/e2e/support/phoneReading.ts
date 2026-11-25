import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from './corsia';
import { inEditorView } from './liveDocument';
import { openFilesButton } from './documentBar';
import { settled } from './tableCells';
import { syntheticPng } from './syntheticImages';

export const READING_DOC = '# Reading notes\n\nOpening passage to select and keep.\n\n| Item | Note |\n|---|---|\n| Alpha | Cell passage |\n| Empty |  |\n\n![Preview](reading-preview.png)\n\n' + Array.from({ length: 35 }, (_, i) => `Passage ${i}: words for a long reading document.\n\n`).join('') + 'Final passage and selection endpoints.\n';

/** Seeds only the disposable e2e workspace, then opens two actual document tabs. */
export async function openReadingDocument(page: Page, width: number, theme: 'light' | 'dark' = 'light') {
	await page.setViewportSize({ width, height: 851 });
	await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
	const params = { token: TEST_TOKEN };
	const title = `reading-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const threadId = await createSession(page.request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await page.request.get('/api/files/tree', { params })).json()).roots;
	const root = roots.find(row => row.kind === 'workspace')!.path;
	fs.writeFileSync(path.join(root, 'reading-preview.png'), syntheticPng(160, 80));
	const tabs = [];
	for (const [suffix, text] of [['notes', READING_DOC], ['other', '# Other notes\n\nOther writing.\n']]) {
		const name = `${title}-${suffix}.md`, docPath = path.join(root, name);
		const response = await page.request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
		expect(response.ok(), await response.text()).toBeTruthy();
		const artifactId = (await response.json()).artifact.id as string;
		tabs.push({ instanceId: `${title}-${suffix}`, artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' });
	}
	await page.addInitScript(({ threadId, tabs }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: {
		[threadId]: { tabs, activeTabId: tabs[0].instanceId, visible: true, activeSurface: 'artifact' },
	}, splitWidth: null })), { threadId, tabs });
	await loginByUrl(page, { waitForComposer: false });
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.doc .cm-content').first()).toContainText('Opening passage', { timeout: 20_000 });
	await settled(page);
	return { title, threadId, tabs, params };
}

/** Uses the actual open-files route for touch tabs and the existing tab strip with a mouse. */
export async function selectReadingTab(page: Page, title: string, touch: boolean) {
	if (touch) {
		await openFilesButton(page, 2).click();
		await page.getByRole('dialog', { name: 'Open side panel items' }).getByRole('button', { name: title, exact: true }).click();
	} else await page.getByRole('tab', { name: title, exact: true }).click();
	await settled(page);
}

/** Scripted CodeMirror source selection, explicitly not a finger-selection proof. */
export async function selectReadingPassage(page: Page, needle: string) {
	await inEditorView(page, (view, words) => {
		const at = view.state.doc.toString().indexOf(words);
		if (at < 0) throw new Error('passage missing');
		view.dispatch({ selection: { anchor: at, head: at + words.length }, effects: view.constructor.scrollIntoView(at, { y: 'center' }) });
	}, needle);
	await expect(page.locator('.sel-comment')).toBeVisible();
}

/**
 * The phone's Comment offer is the document's own floating button, held at the bottom of the visible screen: the same fill, round
 * ends, shadow and text colour the button has beside a selection on a desktop, centred in the document column, with no strip behind it.
 */
export async function expectCommentPill(page: Page) {
	const look = await page.evaluate(() => {
		const holder = document.querySelector<HTMLElement>('.touch-comment-bar.offered')!;
		const button = holder.querySelector<HTMLElement>('button.sel-comment')!;
		// What the button looks like where it floats: a probe carrying the tokens that button is drawn from.
		const probe = document.body.appendChild(document.createElement('div'));
		probe.style.cssText = 'position:fixed;visibility:hidden;background:var(--accent-fill);color:var(--accent-on-fill);border-radius:var(--radius-pill);box-shadow:var(--shadow-chip), var(--glass-rim)';
		const want = getComputedStyle(probe), got = getComputedStyle(button), strip = getComputedStyle(holder);
		const holderBox = holder.getBoundingClientRect(), buttonBox = button.getBoundingClientRect();
		const result = {
			fill: [got.backgroundColor, want.backgroundColor], text: [got.color, want.color], radius: [got.borderTopLeftRadius, want.borderTopLeftRadius], shadow: [got.boxShadow, want.boxShadow],
			stripFill: strip.backgroundColor, stripBorder: strip.borderTopWidth, stripShadow: strip.boxShadow,
			offCentre: Math.abs((buttonBox.left + buttonBox.width / 2) - (holderBox.left + holderBox.width / 2)), icon: !!button.querySelector('svg'),
		};
		probe.remove();
		return result;
	});
	expect(look.fill[0], 'the button is filled with the accent').toBe(look.fill[1]);
	expect(look.text[0], 'the button text is the on-accent colour').toBe(look.text[1]);
	expect(look.radius[0], 'the button has round ends').toBe(look.radius[1]);
	expect(look.shadow[0], 'the button has its shadow').toBe(look.shadow[1]);
	expect(look.icon, 'the button has its comment icon').toBe(true);
	expect(look.stripFill, 'no strip behind the button').toBe('rgba(0, 0, 0, 0)');
	expect(look.stripBorder, 'no line above the button').toBe('0px');
	expect(look.stripShadow, 'no shadow on the holder').toBe('none');
	expect(look.offCentre, 'the button is centred').toBeLessThanOrEqual(1);
}
