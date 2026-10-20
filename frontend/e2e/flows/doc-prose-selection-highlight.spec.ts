import path from 'node:path';
import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test';
import { createSession, e2eModel, loginByUrl, TEST_TOKEN } from '../support/corsia';

const PROSE = 'A visible prose selection must remain painted before a comment starts.';
const PICKED = 'visible prose selection';
const TABLE_TEXT = 'Table text still needs selection.';
const LONG = 'A longer paragraph that wraps across several lines so a drag can hold a multi-line selection while the whole composed document view is captured, because a one-line crop hides how the highlight reads against the page around it and against the lines it does not cover.';
const DOC = [
	'# Prose selection highlight', '',
	PROSE, '',
	'| Capability | Notes |', '| --- | --- |',
	`| Table selection | ${TABLE_TEXT} |`, '',
	'Paragraph after the table.', '',
	LONG, '',
].join('\n');

interface SelectionEvidence {
	text: string;
	rect: { x: number; y: number; width: number; height: number };
	selectionColor: string;
	textColor: string;
}

// The floors the highlight must keep, for the default accent and for a project's: the selection must read against the
// page (1.5, near what common editors use) and the text over it must stay readable (WCAG 1.4.3).
const SELECTION_MIN = 1.5, TEXT_MIN = 4.5;
// Not a swatch's default share: a pale accent that needs more of itself over the light page to reach the floor.
const PROJECT_ACCENT = '#ca8a04';

/** A conversation inside a new project painted `accent`: opening it makes that project's colour the page's accent. */
async function createProjectSession(request: Page['request'], title: string, accent: string) {
	const params = { token: TEST_TOKEN };
	const created = await request.post('/api/projects', { params, data: { name: `${title}-project`, description: 'Prose selection accent proof', status: 'active', accent_color: accent } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const projects = await (await request.get('/api/projects', { params: { ...params, status: 'active' } })).json();
	const project = (projects.projects || []).find((candidate: { name: string }) => candidate.name === `${title}-project`);
	expect(project, 'the created project is listed').toBeTruthy();
	const thread = await request.post('/api/threads', { params, data: { title, project_id: project.id, model: e2eModel() } });
	expect(thread.ok(), await thread.text()).toBeTruthy();
	return { threadId: (await thread.json()).thread_id as string, scopeRoot: project.path as string };
}

async function openDocument(page: Page, theme: 'light' | 'dark', accent?: string) {
	const request = page.request, params = { token: TEST_TOKEN };
	const title = `prose-selection-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
	const project = accent ? await createProjectSession(request, title, accent) : null;
	const threadId = project ? project.threadId : await createSession(request, title);
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots;
	// A project thread may write only inside its project, and not at the project root, which its conductor owns.
	const workspace = project ? path.join(project.scopeRoot, 'docs') : roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`, docPath = path.join(workspace!, fileName);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	await page.addInitScript(({ threadId, docPath, fileName, artifactId, theme }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'prose-tab', artifactId, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'prose-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName, artifactId, theme });
	await loginByUrl(page, { waitForComposer: false });
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	if (accent) {
		// A project's conversations list only inside its scope: enter it from the palette, as a person would.
		await page.getByRole('button', { name: /Jump to/ }).click();
		await page.locator('.palette').getByRole('button', { name: `${title}-project`, exact: true }).click();
		await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
	}
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-md-table table')).toBeVisible({ timeout: 15_000 });
	// The project's colour is the accent only once its scope is entered with the thread; the selection role follows it.
	if (accent) await expect.poll(() => page.evaluate(() => document.documentElement.style.getPropertyValue('--accent'))).toBe(accent);
}

async function prosePoints(page: Page) {
	return page.locator('.cm-line', { hasText: PROSE }).evaluate((line, text) => {
		const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
		const node = walker.nextNode() as Text;
		const start = node.data.indexOf(text);
		if (start < 0) throw new Error(`missing prose ${text}`);
		const point = (offset: number, end = false) => {
			const range = document.createRange();
			range.setStart(node, offset); range.setEnd(node, offset + 1);
			const box = range.getBoundingClientRect();
			return { x: end ? box.right - 1 : box.left + 1, y: box.top + box.height / 2 };
		};
		return { from: point(start), to: point(start + text.length - 1, true) };
	}, PICKED);
}

async function wrappedPoints(page: Page, text: string, startAt: number, endAt: number) {
	return page.locator('.cm-line', { hasText: text }).evaluate((line, { startAt, endAt }) => {
		const node = document.createTreeWalker(line, NodeFilter.SHOW_TEXT).nextNode() as Text;
		const point = (offset: number, end = false) => {
			const range = document.createRange();
			range.setStart(node, offset); range.setEnd(node, offset + 1);
			const box = range.getBoundingClientRect();
			return { x: end ? box.right - 1 : box.left + 1, y: box.top + box.height / 2 };
		};
		return { from: point(startAt), to: point(endAt, true), lineHeight: parseFloat(getComputedStyle(line).lineHeight) };
	}, { startAt, endAt });
}

async function tableTextEnds(page: Page, text: string) {
	return page.evaluate((text) => {
		const cell = [...document.querySelectorAll<HTMLElement>('.cm-md-table td')].find((td) => td.textContent?.includes(text));
		if (!cell) throw new Error(`missing table text ${text}`);
		const start = cell.textContent!.indexOf(text);
		const point = (offset: number, end = false) => {
			const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
			for (let seen = 0, node = walker.nextNode() as Text | null; node; seen += node.data.length, node = walker.nextNode() as Text | null) {
				if (offset < seen + node.data.length) {
					const range = document.createRange();
					range.setStart(node, offset - seen); range.setEnd(node, offset - seen + 1);
					const box = range.getBoundingClientRect();
					return { x: end ? box.right - 1 : box.left + 1, y: box.top + box.height / 2 };
				}
			}
			throw new Error(`offset ${offset} outside table text`);
		};
		return { from: point(start), to: point(start + text.length - 1, true) };
	}, text);
}

async function selectionEvidence(page: Page): Promise<SelectionEvidence> {
	return page.evaluate(() => {
		const selection = document.getSelection();
		if (!selection || selection.rangeCount === 0) throw new Error('no DOM selection');
		const rect = selection.getRangeAt(0).getBoundingClientRect();
		const content = document.querySelector<HTMLElement>('.cm-content');
		if (!content || rect.width <= 0 || rect.height <= 0) throw new Error('selection has no drawable bounds');
		return {
			text: selection.toString(),
			rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
			selectionColor: getComputedStyle(content, '::selection').backgroundColor,
			textColor: getComputedStyle(selection.anchorNode instanceof Element ? selection.anchorNode : selection.anchorNode!.parentElement!).color,
		};
	});
}

/**
 * Measures rendered pixels against the browser-resolved ::selection colour composited over the same pixels before selection.
 * A 20-character selection is mostly its background; requiring at least one fifth avoids passing on anti-aliased edge noise.
 */
async function selectionPaintShare(page: Page, before: Buffer, lineBox: { x: number; y: number; width: number; height: number }, evidence: SelectionEvidence) {
	const after = await page.screenshot({ clip: lineBox, scale: 'css', animations: 'disabled' });
	return page.evaluate(async ({ before, after, lineBox, evidence }) => {
		const decode = async (base64: string) => {
			const image = new Image();
			image.src = `data:image/png;base64,${base64}`;
			await image.decode();
			const canvas = document.createElement('canvas');
			canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
			const context = canvas.getContext('2d', { willReadFrequently: true })!;
			context.drawImage(image, 0, 0);
			return { width: canvas.width, height: canvas.height, pixels: context.getImageData(0, 0, canvas.width, canvas.height).data };
		};
		const [beforeImage, afterImage] = await Promise.all([decode(before), decode(after)]);
		if (beforeImage.width !== afterImage.width || beforeImage.height !== afterImage.height) throw new Error('selection screenshot size changed');
		const swatch = document.createElement('canvas');
		swatch.width = swatch.height = 1;
		const swatchContext = swatch.getContext('2d')!;
		swatchContext.fillStyle = evidence.selectionColor;
		swatchContext.fillRect(0, 0, 1, 1);
		const accent = swatchContext.getImageData(0, 0, 1, 1).data;
		if (accent[3] === 0) throw new Error(`transparent ::selection colour: ${evidence.selectionColor}`);
		const left = Math.max(0, Math.floor(evidence.rect.x - lineBox.x));
		const top = Math.max(0, Math.floor(evidence.rect.y - lineBox.y));
		const right = Math.min(afterImage.width, Math.ceil(evidence.rect.x + evidence.rect.width - lineBox.x));
		const bottom = Math.min(afterImage.height, Math.ceil(evidence.rect.y + evidence.rect.height - lineBox.y));
		let painted = 0, total = 0;
		const alpha = accent[3] / 255;
		for (let y = top; y < bottom; y++) for (let x = left; x < right; x++) {
			const at = (y * afterImage.width + x) * 4;
			const expected = [0, 1, 2].map((channel) => Math.round(accent[channel] * alpha + beforeImage.pixels[at + channel] * (1 - alpha)));
			const distance = Math.max(...expected.map((value, channel) => Math.abs(value - afterImage.pixels[at + channel])));
			const changed = Math.max(...[0, 1, 2].map((channel) => Math.abs(beforeImage.pixels[at + channel] - afterImage.pixels[at + channel])));
			if (distance <= 14 && changed >= 3) painted++;
			total++;
		}
		const median = (values: number[]) => {
			values.sort((a, b) => a - b);
			return values[Math.floor(values.length / 2)];
		};
		const medianPixel = (pixels: Uint8ClampedArray) => [0, 1, 2].map((channel) => {
			const values: number[] = [];
			for (let y = top; y < bottom; y++) for (let x = left; x < right; x++) values.push(pixels[(y * afterImage.width + x) * 4 + channel]);
			return median(values);
		});
		const luminance = (rgb: number[]) => rgb.map((value) => value / 255)
			.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
			.reduce((sum, value, channel) => sum + value * [0.2126, 0.7152, 0.0722][channel], 0);
		const selected = medianPixel(afterImage.pixels), background = medianPixel(beforeImage.pixels);
		const ratio = (a: number[], b: number[]) => {
			const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
			return (light + 0.05) / (dark + 0.05);
		};
		swatchContext.clearRect(0, 0, 1, 1);
		swatchContext.fillStyle = evidence.textColor;
		swatchContext.fillRect(0, 0, 1, 1);
		const text = [...swatchContext.getImageData(0, 0, 1, 1).data].slice(0, 3);
		return { share: painted / total, painted, total, color: evidence.selectionColor, selected, background, contrast: ratio(selected, background), textContrast: ratio(text, selected) };
	}, { before: before.toString('base64'), after: after.toString('base64'), lineBox, evidence });
}

async function assertPaintedSelection(page: Page, lineBox: { x: number; y: number; width: number; height: number }, before: Buffer, expected: string, testInfo: TestInfo, name: string) {
	const evidence = await selectionEvidence(page);
	expect(evidence.text).toBe(expected);
	const clip = { x: Math.max(0, evidence.rect.x), y: Math.max(0, evidence.rect.y), width: Math.max(1, evidence.rect.width), height: Math.max(1, evidence.rect.height) };
	await page.screenshot({ path: testInfo.outputPath(`${name}.png`), clip, scale: 'css', animations: 'disabled' });
	const result = await selectionPaintShare(page, before, lineBox, evidence);
	console.log(`selection-paint ${name}: ${(result.share * 100).toFixed(1)}% (${result.painted}/${result.total}), ${result.color}`);
	console.log(`selection-visibility ${name}: contrast ${result.contrast.toFixed(2)} (${result.selected.join(',')} selected / ${result.background.join(',')} background), text ${result.textContrast.toFixed(2)} (${evidence.textColor})`);
	expect(result.share, `painted share for ${name}`).toBeGreaterThan(0.2);
	expect.soft(result.contrast, `selected vs unselected background contrast for ${name}`).toBeGreaterThanOrEqual(SELECTION_MIN);
	expect.soft(result.textContrast, `text over the selection contrast for ${name}`).toBeGreaterThanOrEqual(TEXT_MIN);
}

async function dragProseAndAssert(page: Page, testInfo: TestInfo, name: string) {
	const line = page.locator('.cm-line', { hasText: PROSE });
	const lineBox = await line.boundingBox();
	expect(lineBox).not.toBeNull();
	const before = await page.screenshot({ clip: lineBox!, scale: 'css', animations: 'disabled' });
	const { from, to } = await prosePoints(page);
	await page.mouse.move(from.x, from.y);
	await page.mouse.down();
	for (let step = 1; step <= 8; step++) await page.mouse.move(from.x + ((to.x - from.x) * step) / 8, from.y + ((to.y - from.y) * step) / 8);
	await assertPaintedSelection(page, lineBox!, before, PICKED, testInfo, `${name}-held`);
	await page.mouse.up();
	await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
	await assertPaintedSelection(page, lineBox!, before, PICKED, testInfo, `${name}-released`);
}

test.describe('@flow:doc-prose-selection-highlight @surface:documents', () => {
	test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name.includes('mobile'), 'a mouse drag'); });

	for (const theme of ['light', 'dark'] as const) {
		test(`${theme}: a prose drag paints while held and after release before Comment`, async ({ page }, testInfo) => {
			test.setTimeout(90_000);
			await page.setViewportSize({ width: 1440, height: 900 });
			await openDocument(page, theme);
			await dragProseAndAssert(page, testInfo, `${theme}-fresh-prose`);
		});

		test(`${theme}: a prose drag still paints after a table-cell drag blurs the editor`, async ({ page }, testInfo) => {
			test.setTimeout(90_000);
			await page.setViewportSize({ width: 1440, height: 900 });
			await openDocument(page, theme);
			const { from, to } = await tableTextEnds(page, TABLE_TEXT);
			await page.mouse.move(from.x, from.y);
			await page.mouse.down();
			for (let step = 1; step <= 8; step++) await page.mouse.move(from.x + ((to.x - from.x) * step) / 8, from.y + ((to.y - from.y) * step) / 8);
			await page.mouse.up();
			await expect.poll(() => page.evaluate(() => {
				const selection = document.getSelection();
				const cellOf = (node: Node | null | undefined) => (node instanceof Element ? node : node?.parentElement)?.closest('.cm-md-table td') ?? null;
				const anchorCell = cellOf(selection?.anchorNode);
				return (selection?.toString().trim().length ?? 0) > 0 && !!anchorCell && anchorCell === cellOf(selection?.focusNode);
			})).toBe(true);
			await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
			await dragProseAndAssert(page, testInfo, `${theme}-after-table-prose`);
		});

		test(`${theme}: a held multi-line drag reads in the composed document view`, async ({ page }, testInfo) => {
			test.setTimeout(90_000);
			await page.setViewportSize({ width: 1440, height: 900 });
			await openDocument(page, theme);
			const lineBox = await page.locator('.cm-line', { hasText: LONG }).boundingBox();
			expect(lineBox).not.toBeNull();
			const before = await page.screenshot({ clip: lineBox!, scale: 'css', animations: 'disabled' });
			const { from, to, lineHeight } = await wrappedPoints(page, LONG, 12, LONG.length - 20);
			await page.mouse.move(from.x, from.y);
			await page.mouse.down();
			for (let step = 1; step <= 12; step++) await page.mouse.move(from.x + ((to.x - from.x) * step) / 12, from.y + ((to.y - from.y) * step) / 12);
			const evidence = await selectionEvidence(page);
			expect(evidence.rect.height, 'the held selection spans several lines').toBeGreaterThan(lineHeight * 2);
			expect(LONG).toContain(evidence.text);
			await assertPaintedSelection(page, lineBox!, before, evidence.text, testInfo, `${theme}-multiline-held`);
			await page.screenshot({ path: testInfo.outputPath(`${theme}-multiline-held-full.png`), scale: 'css', animations: 'disabled' });
			await page.mouse.up();
		});
	}

	test(`light, project accent ${PROJECT_ACCENT}: a prose drag reads against the page`, async ({ page }, testInfo) => {
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 1440, height: 900 });
		await openDocument(page, 'light', PROJECT_ACCENT);
		await dragProseAndAssert(page, testInfo, 'light-project-accent-fresh-prose');
	});
});
