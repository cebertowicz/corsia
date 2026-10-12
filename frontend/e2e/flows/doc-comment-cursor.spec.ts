import path from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

type SeedComment = { anchor: string; body: string };
type LineClick = {
	marker: string;
	note: string;
	/** Height within the line box to click; the middle unless given. */
	fraction?: number;
	/** Click the centre of this visual row of a wrapped line instead. */
	row?: number;
	/** False clicks the line where it already is, without scrolling to it first. */
	scroll?: boolean;
};

// Where a scrolled-to line rests, as a fraction of the editor's height.
const PLACE = 0.3;
// How far, in pixels, the scroll position may differ across a click: rounding only.
const SCROLL_TOLERANCE = 1;
// Every case runs with ordinary transitions and with the reduced-motion setting.
const MOTIONS = [
	{ name: 'default motion', reducedMotion: 'no-preference' },
	{ name: 'reduced motion', reducedMotion: 'reduce' },
] as const;
type Motion = (typeof MOTIONS)[number];

/** Requirement sections that pad a document to PRD length; every fourth carries a code block. */
function filler(first: number, count: number): string[] {
	return Array.from({ length: count }, (_, offset) => {
		const n = first + offset;
		return [
			`## Requirement ${n}`,
			'',
			`Requirement ${n} states the behaviour in enough words that the paragraph wraps onto a second row in the document canvas, the way a real requirements document reads.`,
			'',
			`- Acceptance ${n}.1 holds at every width.`,
			`- Acceptance ${n}.2 holds after a reload.`,
			'',
			...(n % 4 === 0 ? ['```sh', `corsia check --requirement ${n}`, '```', ''] : []),
		];
	}).flat();
}

/** The lines under test. Each is short enough to stay on one row at 393px and carries a unique marker. */
function targets(id: string): string[] {
	return [
		`### Detail qx-${id}-head`,
		'',
		`Plain line qx-${id}-plain here.`,
		'',
		'```ts',
		'const ready = true;',
		`// qx-${id}-incode`,
		'```',
		'',
		`After code qx-${id}-code here.`,
		'',
		`- qx-${id}-own has a reviewed ${id} note.`,
		`- Next item qx-${id}-cmt here.`,
		'',
	];
}

function lineClicks(id: string, where: string): LineClick[] {
	return [
		{ marker: `qx-${id}-head`, note: `heading, ${where}` },
		{ marker: `qx-${id}-plain`, note: `paragraph, ${where}` },
		{ marker: `qx-${id}-plain`, note: `upper part of a paragraph, ${where}`, fraction: 0.2 },
		{ marker: `qx-${id}-plain`, note: `lower part of a paragraph, ${where}`, fraction: 0.8 },
		{ marker: `qx-${id}-incode`, note: `inside a code block, ${where}` },
		{ marker: `qx-${id}-code`, note: `right after a code block, ${where}` },
		{ marker: `qx-${id}-own`, note: `commented line, outside its highlight, ${where}` },
		{ marker: `qx-${id}-cmt`, note: `right after a commented line, ${where}` },
	];
}

/** Seeds the document and its comments, opens it at this project's viewport and returns a label for that viewport and motion setting. */
async function openDocument(page: Page, testInfo: TestInfo, motion: Motion, name: string, text: string, comments: SeedComment[]): Promise<string> {
	const size = testInfo.project.name.includes('mobile') ? { width: 393, height: 852 } : { width: 1440, height: 960 };
	await page.setViewportSize(size);
	await page.emulateMedia({ reducedMotion: motion.reducedMotion });
	const request = page.request;
	const title = `${name}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const token = encodeURIComponent(TEST_TOKEN);
	const rootsResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text, thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId: string = (await seeded.json()).artifact.id;
	for (const comment of comments) {
		// A highlight renders only while its offsets still hold the quoted text.
		const from = text.indexOf(comment.anchor);
		expect(from, `setup: "${comment.anchor}" is in the document`).toBeGreaterThanOrEqual(0);
		const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${token}`, {
			data: { body: comment.body, anchor_text: comment.anchor, from_off: from, to_off: from + comment.anchor.length },
		});
		expect(created.ok(), await created.text()).toBeTruthy();
	}
	await page.addInitScript(({ threadId, docPath, fileName }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'cursor-tab', artifactId: null, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'cursor-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: null,
		}));
	}, { threadId, docPath, fileName });
	await loginByUrl(page);
	if (size.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toBeVisible();
	await expect(page.getByRole('button', { name: commentsButton(comments.length) })).toBeEnabled();
	// The editor mounts before the text arrives; no line can be found until it has, and scrolls in the editor.
	const opening = text.split('\n')[0].replace(/^#+ /, '');
	await expect.poll(() => page.locator('.cm-scroller').evaluate((scroller, opening) => ({
		text: scroller.querySelector('.cm-line')?.textContent?.includes(opening) ?? false,
		scrollsInEditor: scroller.scrollHeight > scroller.clientHeight * 2,
	}), opening), { message: 'setup: the document text is loaded and scrolls inside the editor' }).toEqual({ text: true, scrollsInEditor: true });
	// Wait out the sidebar and canvas transitions: an in-flight slide, or any other transform, scale
	// or zoom on an ancestor, shifts every coordinate.
	await expect.poll(() => page.locator('.cm-editor').evaluate((node) => {
		const transformed: string[] = [];
		for (let current: Element | null = node; current; current = current.parentElement) {
			const style = getComputedStyle(current);
			if ((style.transform !== 'none' && !new DOMMatrixReadOnly(style.transform).isIdentity) || style.scale !== 'none' || style.zoom !== '1') {
				transformed.push(`${current.tagName.toLowerCase()}.${current.getAttribute('class') ?? ''}`);
			}
		}
		return { transformed, runningAnimations: document.getAnimations().filter((animation) => animation.playState === 'running').length };
	}), { message: 'setup: animations have finished and no ancestor of the editor is transformed' }).toEqual({ transformed: [], runningAnimations: 0 });
	return `${size.width}x${size.height}, ${motion.name}`;
}

function commentsButton(count: number): RegExp {
	return new RegExp(`^${count} open thread${count === 1 ? '' : 's'}\\b`);
}

/**
 * Finds the line holding `marker` and waits until it stops moving. With
 * `scroll` it first brings the line to rest at PLACE; CodeMirror renders only
 * the lines near the viewport, so a line not yet in the DOM is paged to from
 * the top. A line that is never found comes back with what the search saw.
 */
async function locateLine(page: Page, marker: string, scroll: boolean) {
	return page.locator('.cm-scroller').evaluate(async (scroller, { marker, scroll, place }) => {
		const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const find = () => [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((line) => line.textContent?.includes(marker));
		let pages = 0;
		const trace: string[] = [];
		const note = (phase: string) => { if (trace.length < 80) trace.push(`${phase}:${Math.round(scroller.scrollTop)}/${scroller.scrollHeight}/${scroller.querySelectorAll('.cm-line').length}`); };
		note(scroller.isConnected ? 'start' : 'detached');
		if (scroll && !find()) {
			scroller.scrollTop = 0;
			for (; pages < 400; pages += 1) {
				await frame();
				await frame();
				note('page');
				if (find()) break;
				// The end of the document; its height is an estimate until the lines above are measured.
				if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1) break;
				scroller.scrollTop += scroller.clientHeight * 0.75;
			}
		}
		let still = 0;
		let last = Number.NaN;
		for (let pass = 0; pass < 180 && still < 3; pass += 1) {
			const line = find();
			note(line ? `rest@${Math.round(line.getBoundingClientRect().top)}` : 'lost');
			if (!line) break;
			const top = line.getBoundingClientRect().top;
			const wanted = scroller.getBoundingClientRect().top + scroller.clientHeight * place;
			const before = scroller.scrollTop;
			if (scroll && Math.abs(top - wanted) > 1) scroller.scrollTop = before + top - wanted;
			still = scroller.scrollTop === before && top === last ? still + 1 : 0;
			last = top;
			await frame();
		}
		const line = find();
		const rendered = [...scroller.querySelectorAll<HTMLElement>('.cm-line')];
		const rect = (line ?? scroller).getBoundingClientRect();
		const box = scroller.getBoundingClientRect();
		const rowHeight = line ? Number.parseFloat(getComputedStyle(line).lineHeight) : Number.NaN;
		return {
			found: !!line,
			still: still >= 3,
			onScreen: !!line && rect.top >= box.top && rect.bottom <= box.bottom,
			left: rect.left, top: rect.top, bottom: rect.bottom,
			rowHeight: Number.isFinite(rowHeight) ? rowHeight : rect.height,
			scrollTop: scroller.scrollTop,
			search: {
				pages, trace, scrollHeight: scroller.scrollHeight, clientHeight: scroller.clientHeight, lines: rendered.length,
				firstLine: rendered[0]?.textContent?.slice(0, 40) ?? null, lastLine: rendered.at(-1)?.textContent?.slice(0, 40) ?? null,
			},
		};
	}, { marker, scroll, place: PLACE });
}

/**
 * Reads the caret the way a user sees it: the browser's own selection and the
 * rectangles of the rendered lines. `frames` is how long to let the page settle
 * before reading.
 */
async function readCaret(page: Page, y: number, frames = 2) {
	return page.locator('.cm-scroller').evaluate(async (scroller, { y, frames }) => {
		const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const content = scroller.querySelector('.cm-content')!;
		const describe = (element: Element | null) => element ? `${element.tagName.toLowerCase()}.${element.getAttribute('class') ?? ''}` : null;
		const inEditor = () => {
			const node = window.getSelection()?.focusNode;
			return !!node && content.contains(node);
		};
		for (let wait = 0; wait < 30 && !inEditor(); wait += 1) await frame();
		for (let wait = 0; wait < frames; wait += 1) await frame();
		const selection = window.getSelection();
		const node = selection?.focusNode ?? null;
		const element = node instanceof Element ? node : node?.parentElement ?? null;
		const line = inEditor() ? element?.closest<HTMLElement>('.cm-line') ?? null : null;
		const lineRect = line?.getBoundingClientRect() ?? null;
		// A collapsed range has no rectangle at some element boundaries; the line's own box stands in.
		const row = (line && selection?.rangeCount ? [...selection.getRangeAt(0).getClientRects()].find((rect) => rect.height > 0) : null) ?? lineRect;
		return {
			caretLine: line?.textContent ?? null,
			selectionIn: describe(element),
			rowTop: row?.top ?? null, rowBottom: row?.bottom ?? null,
			scrollTopAfter: scroller.scrollTop,
			focus: describe(document.activeElement),
			// Rendered tables and diagrams above the click, with the margins the layout gives them.
			widgetsAbove: [...content.querySelectorAll<HTMLElement>('.cm-md-table, .cm-md-mermaid')].filter((widget) => widget.getBoundingClientRect().bottom <= y).map((widget) => {
				const style = getComputedStyle(widget);
				return `${widget.classList[0]} height ${widget.getBoundingClientRect().height.toFixed(1)} margin ${style.marginTop} ${style.marginBottom}`;
			}),
		};
	}, { y, frames });
}

/** Clicks a line and expects the caret on that line, on the visual row under the pointer. */
async function clickLine(page: Page, viewport: string, click: LineClick): Promise<void> {
	const label = `[${viewport}] ${click.marker} (${click.note})`;
	const line = await locateLine(page, click.marker, click.scroll ?? true);
	if (!line.found || !line.still || !line.onScreen) {
		expect.soft(line, `setup: ${label} line is on screen and still`).toMatchObject({ found: true, still: true, onScreen: true });
		return;
	}
	const x = line.left + 40;
	const y = click.row ? line.top + (click.row - 0.5) * line.rowHeight : line.top + (line.bottom - line.top) * (click.fraction ?? 0.5);
	const under = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest('.cm-line')?.textContent ?? '(no document line under the click point)', { x, y });
	if (!under.includes(click.marker)) {
		expect.soft(under, `setup: ${label} click point is on the target line`).toContain(click.marker);
		return;
	}
	await page.mouse.click(x, y);
	const caret = await readCaret(page, y);
	const clicked = `clicked at y=${y.toFixed(1)} in the line at ${line.top.toFixed(1)}-${line.bottom.toFixed(1)}`;
	// The clicked line is fully on screen, so the click has nothing to scroll into view.
	expect.soft(Math.abs(caret.scrollTopAfter - line.scrollTop), `page jump: ${label} ${clicked}; the scroll position went from ${line.scrollTop} to ${caret.scrollTopAfter}`).toBeLessThanOrEqual(SCROLL_TOLERANCE);
	const above = caret.widgetsAbove.length ? `; above the click: ${caret.widgetsAbove.join(', ')}` : '';
	expect.soft(caret.caretLine ?? `(caret is not in a document line; the selection is in ${caret.selectionIn})`, `caret: ${label} ${clicked}${above}`).toContain(click.marker);
	if (caret.caretLine?.includes(click.marker)) {
		expect.soft(caret.rowTop! - 4 <= y && y <= caret.rowBottom! + 4, `caret row: ${label} ${clicked}, caret row at ${caret.rowTop?.toFixed(1)}-${caret.rowBottom?.toFixed(1)}`).toBe(true);
	}
}

/**
 * Clicks inside commented text. The click also opens that comment, which takes
 * the keyboard focus and can move the line under the pointer; the caret must
 * still be in the clicked line.
 */
async function clickCommentedText(page: Page, viewport: string, marker: string): Promise<void> {
	const label = `[${viewport}] ${marker} (inside commented text)`;
	const line = await locateLine(page, marker, true);
	// Read without waiting: a highlight that is not there is a setup fault to report, not to wait out.
	const highlight = await page.locator('.cm-scroller').evaluate((scroller, marker) => {
		const target = [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((candidate) => candidate.textContent?.includes(marker));
		const rect = target?.querySelector('.cm-comment-anchor')?.getBoundingClientRect();
		return {
			found: !!rect, x: rect?.x ?? 0, y: rect?.y ?? 0, width: rect?.width ?? 0, height: rect?.height ?? 0,
			highlightsRendered: scroller.querySelectorAll('.cm-comment-anchor').length,
		};
	}, marker);
	if (!line.found || !line.still || !line.onScreen || !highlight.found) {
		expect.soft({ line, highlight }, `setup: ${label} highlight is on screen and still`).toMatchObject({ line: { found: true, still: true, onScreen: true }, highlight: { found: true } });
		return;
	}
	const x = highlight.x + highlight.width / 2;
	const y = highlight.y + highlight.height / 2;
	await page.mouse.click(x, y);
	const caret = await readCaret(page, y, 30);
	expect(caret.caretLine ?? `(caret is not in a document line; the selection is in ${caret.selectionIn}, the focus is on ${caret.focus})`, `caret: ${label}`).toContain(marker);
}

for (const motion of MOTIONS) {
	test(`@surface:doc-comment-cursor caret lands on the clicked line in a long commented document (${motion.name})`, async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const text = [
			'# Caret requirements',
			'',
			'This document is long enough that the lines under test sit well below the fold.',
			'',
			...filler(1, 12),
			...targets('a'),
			'Wrapped line qx-a-wrap repeats enough ordinary words to run onto a second visual row in the document canvas at any width, so a click on that row has to leave the caret there.',
			'',
			...filler(13, 6),
		].join('\n');
		const comments = [
			{ anchor: 'Acceptance 3.1 holds', body: 'Check this acceptance.' },
			{ anchor: 'Requirement 9 states the behaviour', body: 'Tighten this wording.' },
			{ anchor: 'reviewed a note', body: 'Reviewed in the last pass.' },
		];
		const viewport = await openDocument(page, testInfo, motion, 'caret-commented', text, comments);
		for (const click of lineClicks('a', 'no table or diagram above')) await clickLine(page, viewport, click);
		await clickLine(page, viewport, { marker: 'qx-a-wrap', note: 'second row of a wrapped line', row: 2 });

		// Opening the comments list resizes the editor; the first click lands before anything scrolls.
		await locateLine(page, 'qx-a-plain', true);
		await page.getByRole('button', { name: commentsButton(comments.length) }).click();
		const list = page.getByRole('region', { name: 'Document comments', exact: true });
		await expect(list).toBeVisible();
		// A canvas too narrow for a side column opens the list as a sheet over the document, which the editor does not resize for. Where
		// that sheet covers the place a clicked line rests at, the document cannot be clicked until it closes; elsewhere the clicks land.
		const sheetCovers = await page.evaluate((place) => {
			const scroller = document.querySelector('.cm-scroller')!.getBoundingClientRect();
			const hit = document.elementFromPoint(scroller.left + scroller.width / 2, scroller.top + scroller.height * place);
			return !!hit?.closest('.canvas-comment-drawer.sheet');
		}, PLACE);
		if (!sheetCovers) {
			await clickLine(page, viewport, { marker: 'qx-a-plain', note: 'comments list just opened, nothing scrolled', scroll: false });
			for (const marker of ['qx-a-code', 'qx-a-own', 'qx-a-cmt']) await clickLine(page, viewport, { marker, note: 'comments list open' });
		}
		await list.getByRole('button', { name: 'Close comments', exact: true }).click();
		await expect(list).toHaveCount(0);

		await clickCommentedText(page, viewport, 'qx-a-own');
	});

	test(`@surface:doc-comment-cursor caret lands on the clicked line below a rendered table (${motion.name})`, async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const text = [
			'# Caret below a table',
			'',
			'The lines under test sit right below a rendered table, then far below it.',
			'',
			...filler(1, 8),
			'Above table qx-t-above here.',
			'',
			'| Area | Owner | State |',
			'| --- | --- | --- |',
			'| Editor | Documents | Ready |',
			'| Comments | Documents | Ready |',
			'',
			...targets('b'),
			// Enough sections that the table is no longer rendered when the last group is on screen.
			...filler(9, 40),
			...targets('c'),
			...filler(49, 4),
		].join('\n');
		const viewport = await openDocument(page, testInfo, motion, 'caret-table', text, [
			{ anchor: 'reviewed b note', body: 'Reviewed below the table.' },
			{ anchor: 'reviewed c note', body: 'Reviewed far below the table.' },
		]);
		await clickLine(page, viewport, { marker: 'qx-t-above', note: 'right above the table' });
		await expect(page.locator('.cm-md-table table'), 'setup: the table rendered').toBeVisible();
		for (const click of lineClicks('b', 'below a rendered table')) await clickLine(page, viewport, click);
		for (const click of lineClicks('c', 'far below the table')) await clickLine(page, viewport, click);
	});

	test(`@surface:doc-comment-cursor caret lands on the clicked line below a rendered diagram (${motion.name})`, async ({ page }, testInfo) => {
		test.setTimeout(120_000);
		const text = [
			'# Caret below a diagram',
			'',
			'A short flow, then the lines under test.',
			'',
			'```mermaid',
			'flowchart TD',
			'  Draft --> Review',
			'  Review --> Publish',
			'```',
			'',
			'First line qx-m-first here.',
			'',
			...targets('m'),
			...filler(1, 12),
		].join('\n');
		const viewport = await openDocument(page, testInfo, motion, 'caret-diagram', text, [
			{ anchor: 'reviewed m note', body: 'Reviewed below the diagram.' },
		]);
		await expect(page.locator('.cm-md-mermaid svg'), 'setup: the diagram rendered').toBeVisible({ timeout: 20_000 });
		// The diagram paints after the editor first measured it; this click lands before anything scrolls.
		await clickLine(page, viewport, { marker: 'qx-m-first', note: 'diagram just painted, nothing scrolled', scroll: false });
		// Scrolling away and back lets the editor measure the painted diagram.
		await locateLine(page, 'Requirement 12', true);
		await clickLine(page, viewport, { marker: 'qx-m-first', note: 'right below a diagram' });
		for (const click of lineClicks('m', 'below a rendered diagram')) await clickLine(page, viewport, click);
	});
}
