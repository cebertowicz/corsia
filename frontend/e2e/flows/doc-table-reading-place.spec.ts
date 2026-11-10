import fs from 'node:fs';
import { expect, type Browser, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createComment, openLiveDocument, seedLiveDocument, type LiveDocument } from '../support/liveDocument';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';
import { pill, widen, type Doc } from '../support/marginCards';

/**
 * A table-heavy document stays still under the reader: a status page of wide tables whose rows wrap onto several lines.
 * Another writer's edits to a row above the one the reader has at the top, inside the same table, to another table, or
 * below, leave that row where it is on screen; and clicking the commented bold title of a row leaves the row in view.
 */

const CELL = 'the owning lane reports progress here with enough words that the cell wraps onto several lines at every width';
const row = (key: string) => `| **${key} title:** ${CELL} | Lane ${key} | M | ${key} status: on track, ${CELL} | ${key.length * 7} |`;
const HEAD = ['| Deliverable | Owner | Effort | Status | Score |', '|---|---|---|---|---|'];
const table = (name: string, rows = 6) => [...HEAD, ...Array.from({ length: rows }, (_, i) => row(`${name}-r${i}`))].join('\n');

/** BRIEFING-shaped: a short list, then sections of five-column tables. Public-safe filler only. */
function body(): string {
	const out = ['# Briefing', '', '**Updated:** a morning in November', '', '## Needs you', ''];
	for (let i = 1; i <= 4; i++) out.push(`${i}. **Decision ${i}:** ${CELL}.`);
	out.push('', '## Punch list', '');
	for (const round of [1, 2, 3]) {
		for (const name of ['first', 'steer', 'learn', 'auto', 'trust']) {
			out.push(`### ${name} round ${round}`, '', table(`${name}${round}`), '');
		}
	}
	return out.join('\n');
}

interface Sample { t: number; top: number | null; x: number | null; scrollTop: number; raw: number }
interface Drift {
	name: string; maxDrift: number; finalDrift: number; maxXDrift: number; scrollTopBefore: number; scrollTopAfter: number;
	frames: number; missing: number; rawFrames: number;
	/** Each frame whose row or offset differs from the frame before: ms, row top, scroll offset. */
	trace: Array<[number, number | null, number]>;
}
type Win = Window & { __eachPaintedFrame?: EachPaintedFrame; __rowSamples?: Sample[]; __rowDone?: boolean };

const editorView = `(() => { const content = document.querySelector('.cm-content'); return content && content.cmTile && content.cmTile.root.view; })()`;
const LAYOUTS = [[1440, 900], [393, 852]] as const;

/** Where the table row holding `key` is on screen (its top and left), in the next painted frame. */
async function rowAt(page: Page, key: string): Promise<{ top: number; x: number } | null> {
	return page.evaluate((key) => new Promise<{ top: number; x: number } | null>((resolve) => {
		(window as Win).__eachPaintedFrame!(() => {
			const tr = [...document.querySelectorAll<HTMLElement>('.cm-md-table tr')].find((el) => el.textContent?.includes(`${key} title:`));
			const box = tr?.getBoundingClientRect();
			return box ? { top: box.top, x: box.left } : null;
		}, (at) => { resolve(at); return false; });
	}), key);
}

/** Scrolls as a reader would so the row holding `key` starts (or with `edge` 'bottom', ends) `margin` px below the scroller's top, then lets it settle. */
async function readAtRow(page: Page, key: string, margin = 40, edge: 'top' | 'bottom' = 'top'): Promise<void> {
	for (let pass = 0; pass < 4; pass++) {
		await page.evaluate(({ key, margin, edge }) => {
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
			const tr = [...document.querySelectorAll<HTMLElement>('.cm-md-table tr')].find((el) => el.textContent?.includes(`${key} title:`));
			if (tr) { scroller.scrollTop += tr.getBoundingClientRect()[edge] - scroller.getBoundingClientRect().top - margin; return; }
			// Not drawn yet: bring its table in through the editor first.
			const view = eval(`(() => { const content = document.querySelector('.cm-content'); return content && content.cmTile && content.cmTile.root.view; })()`);
			const at = view.state.doc.toString().indexOf(`**${key} title:**`);
			view.dispatch({ effects: view.constructor.scrollIntoView(at, { y: 'center' }) });
		}, { key, margin, edge });
		await page.waitForTimeout(400);
	}
}

async function startSampling(page: Page, key: string, ms: number): Promise<void> {
	await page.evaluate(({ key, ms }) => {
		const win = window as Win;
		win.__rowSamples = [];
		win.__rowDone = false;
		const start = performance.now();
		win.__eachPaintedFrame!(() => {
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
			const tr = [...document.querySelectorAll<HTMLElement>('.cm-md-table tr')].find((el) => el.textContent?.includes(`${key} title:`));
			const box = tr?.getBoundingClientRect();
			// A table drawn as its source for a frame shows its pipe rows as text lines.
			const raw = [...scroller.querySelectorAll<HTMLElement>('.cm-line')].filter((line) => (line.textContent ?? '').trimStart().startsWith('|')).length;
			return { t: performance.now() - start, top: box ? box.top : null, x: box ? box.left : null, scrollTop: scroller.scrollTop, raw };
		}, (sample) => {
			win.__rowSamples!.push(sample);
			if (sample.t < ms) return true;
			win.__rowDone = true;
			return false;
		});
	}, { key, ms });
}

const tenth = (n: number) => Math.round(n * 10) / 10;

async function drift(page: Page, name: string, anchor: { top: number; x: number }): Promise<Drift> {
	await expect.poll(() => page.evaluate(() => (window as Win).__rowDone), { timeout: 30_000 }).toBe(true);
	const samples = await page.evaluate(() => (window as Win).__rowSamples!);
	const seen = samples.filter((s) => s.top !== null);
	const deltas = seen.map((s) => s.top! - anchor.top);
	return {
		name,
		maxDrift: tenth(Math.max(0, ...deltas.map(Math.abs))),
		finalDrift: tenth(deltas.at(-1) ?? NaN),
		maxXDrift: tenth(Math.max(0, ...seen.map((s) => Math.abs(s.x! - anchor.x)))),
		scrollTopBefore: tenth(samples[0].scrollTop),
		scrollTopAfter: tenth(samples.at(-1)!.scrollTop),
		frames: samples.length,
		missing: samples.length - seen.length,
		rawFrames: samples.filter((s) => s.raw > 0).length,
		trace: samples.filter((s, i) => i === 0 || s.top !== samples[i - 1].top || s.scrollTop !== samples[i - 1].scrollTop)
			.map((s) => [Math.round(s.t), s.top === null ? null : tenth(s.top), tenth(s.scrollTop)]),
	};
}

/** The writer replaces the text after `find` (its first occurrence) with `insert`; the reader applies the broadcast. */
async function writerEdits(writer: Page, find: string, insert: string, remove = 0): Promise<void> {
	await writer.evaluate(({ editorView, find, insert, remove }) => {
		const view = eval(editorView);
		const at = view.state.doc.toString().indexOf(find);
		if (at < 0) throw new Error(`writer cannot find ${find}`);
		const from = at + find.length;
		view.dispatch({ changes: { from, to: from + remove, insert } });
	}, { editorView, find, insert, remove });
}

const LONGER = ' and a longer status line that the agent wrote just now, long enough to add rows of wrapped text to this cell';

async function pair(browser: Browser, document: LiveDocument, width: number, height: number) {
	const writerContext = await browser.newContext({ viewport: { width, height } });
	const readerContext = await browser.newContext({ viewport: { width, height } });
	const writer = await writerContext.newPage();
	const reader = await readerContext.newPage();
	await openLiveDocument(writer, document, '# Briefing');
	await openLiveDocument(reader, document, '# Briefing');
	return { writer, reader, close: async () => { await writerContext.close(); await readerContext.close(); } };
}

const READ = 'steer2-r3';
/** `read`: the row scrolled to, how far below the edge it starts (or ends); `watch`: the row whose place is measured (default both READ). */
const editCases: Array<{ name: string; find: string; insert: string; remove?: number; read?: [string, number, ('top' | 'bottom')?]; watch?: string }> = [
	{ name: 'row above in the same table grows', find: 'steer2-r1 status: on track,', insert: LONGER },
	{ name: 'row inserted above in the same table', find: `${row('steer2-r0')}\n`, insert: `${row('steer2-new')}\n` },
	{ name: 'row above in the same table shrinks', find: 'steer2-r2 status: on track,', insert: ' done.', remove: ` ${CELL}`.length },
	{ name: 'row in a table above grows', find: 'first2-r4 status: on track,', insert: LONGER },
	{ name: 'row below in the same table grows', find: 'steer2-r5 status: on track,', insert: LONGER },
	{ name: 'row in a table below grows', find: 'learn2-r2 status: on track,', insert: LONGER },
	// The edge cuts a table's last row, which ends just inside the view: the reader has the text after the table, which stays
	// put when that row grows. A table no case above edits, so the row is short enough to end in view at every width.
	{ name: 'last row cut off at the edge grows', find: 'auto2-r5 status: on track,', insert: LONGER, read: ['auto2-r5', 60, 'bottom'], watch: 'trust2-r0' },
];

for (const [width, height] of LAYOUTS) {
	test(`@surface:live-document edits around a table row being read leave it in place at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip((width <= 768) !== testInfo.project.name.endsWith('-mobile'), 'One layout per project.');
		test.setTimeout(240_000);
		const document = await seedLiveDocument(request, `table-read-${width}`, body());
		const live = await pair(browser, document, width, height);
		const results: Drift[] = [];
		try {
			await installPaintedFrames(live.reader);
			for (const { name, find, insert, remove, read = [READ, 40], watch = READ } of editCases) {
				await readAtRow(live.reader, ...read);
				const anchor = await rowAt(live.reader, watch);
				expect(anchor, `${name}: the row being read is drawn`).not.toBeNull();
				await startSampling(live.reader, watch, 2_500);
				await live.reader.screenshot({ path: testInfo.outputPath(`table-${width}-${name.replace(/ /g, '-')}-before.png`) });
				await writerEdits(live.writer, find, insert, remove);
				const result = await drift(live.reader, name, anchor!);
				await live.reader.screenshot({ path: testInfo.outputPath(`table-${width}-${name.replace(/ /g, '-')}-after.png`) });
				results.push(result);
			}
		} finally {
			fs.writeFileSync(testInfo.outputPath(`table-drift-${testInfo.project.name}-${width}.json`), JSON.stringify(results, null, 2));
			testInfo.annotations.push({ type: 'drift', description: JSON.stringify(results) });
			await live.close();
		}
		for (const result of results) {
			expect.soft(result.maxDrift, `${result.name}: the row in view never moves`).toBeLessThanOrEqual(2);
			expect.soft(result.maxXDrift, `${result.name}: the row in view never moves sideways`).toBeLessThanOrEqual(2);
			expect.soft(result.rawFrames, `${result.name}: tables never flash to their source`).toBe(0);
			// A frame without the row means the view left it: a table dropped out of the drawn area and the text under the reader changed.
			expect.soft(result.missing, `${result.name}: the row being read stays drawn in every frame`).toBe(0);
		}
	});
}

interface Placement { scrollTop: number; rowTop: number; rowBottom: number; rowX: number; textTop: number; textX: number; bandTop: number; bandBottom: number }

/** The scroller's offset, the commented row's box and the part of the scroller on screen (a phone sheet covers its bottom). */
async function placement(page: Page, key: string): Promise<Placement> {
	return page.evaluate((key) => {
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const tr = [...document.querySelectorAll<HTMLElement>('.cm-md-table tr')].find((el) => el.textContent?.includes(`${key} title:`))!;
		const box = scroller.getBoundingClientRect(), rect = tr.getBoundingClientRect(), text = tr.querySelector('strong')!.getBoundingClientRect();
		// At phone width the comments come up as a sheet over the text; beside a wide canvas they sit in the margin.
		const sheet = window.innerWidth <= 768 ? document.querySelector<HTMLElement>('[data-doc-comments-drawer]') : null;
		const bottom = sheet ? Math.min(box.bottom, sheet.getBoundingClientRect().top) : box.bottom;
		return { scrollTop: scroller.scrollTop, rowTop: rect.top, rowBottom: rect.bottom, rowX: rect.left, textTop: text.top, textX: text.left, bandTop: box.top, bandBottom: bottom };
	}, key);
}

const CLICKED = 'steer2-r4';

// popover: the default split, too narrow for the column; margin: the column already up; opens: a canvas with room for the
// column, closed, so the click opens it and the text narrows; sheet: a phone.
const CLICK_LAYOUTS = [[1440, 900, 'popover'], [1440, 900, 'margin'], [1440, 900, 'opens'], [393, 852, 'sheet']] as const;

for (const [width, height, cards] of CLICK_LAYOUTS) {
	test(`@surface:live-document clicking a commented row title keeps the row in view at ${width} (${cards})`, async ({ browser, request }, testInfo) => {
		test.skip((width <= 768) !== testInfo.project.name.endsWith('-mobile'), 'One layout per project.');
		test.setTimeout(120_000);
		const document = await seedLiveDocument(request, `table-click-${width}`, body());
		await createComment(request, document, `${CLICKED} title:`, 'Is this row right?');
		await createComment(request, document, 'steer2-r2 title:', 'And this one?');
		const context = await browser.newContext({ viewport: { width, height } });
		const results: Array<{ step: string } & Placement> = [];
		try {
			const page = await context.newPage();
			await openLiveDocument(page, document, '# Briefing');
			// The margin column needs a canvas wide enough for it; at the default split a thread opens as a popover.
			if (cards === 'margin' || cards === 'opens') {
				await widen(page, document as unknown as Doc);
				if (cards === 'opens') {
					await page.getByRole('button', { name: 'Close comments' }).click();
					await expect(page.locator('.canvas-comment-drawer.margin')).toHaveCount(0);
				}
				// The column opening holds the top line until the reader takes over; a reader scrolls with the wheel, which ends it.
				await page.locator('.cm-scroller').hover();
				await page.mouse.wheel(0, 40);
			}
			await readAtRow(page, CLICKED, Math.round(height / 3));
			results.push({ step: 'before', ...await placement(page, CLICKED) });
			await page.screenshot({ path: testInfo.outputPath(`click-${width}-${cards}-before.png`) });
			await page.locator('.cm-md-table tr', { hasText: `${CLICKED} title:` }).locator('strong').first().click();
			for (const ms of [100, 400, 1_500]) {
				await page.waitForTimeout(ms);
				results.push({ step: `after ${ms} ms`, ...await placement(page, CLICKED) });
			}
			await page.screenshot({ path: testInfo.outputPath(`click-${width}-${cards}-after.png`) });
		} finally {
			fs.writeFileSync(testInfo.outputPath(`table-click-${testInfo.project.name}-${width}-${cards}.json`), JSON.stringify(results, null, 2));
			testInfo.annotations.push({ type: 'click', description: JSON.stringify(results) });
			await context.close();
		}
		const first = results[0], last = results.at(-1)!;
		expect(last.textTop, 'the clicked title is not above the view').toBeGreaterThanOrEqual(last.bandTop - 1);
		expect(last.textTop + 24, 'the clicked title is not below the view').toBeLessThanOrEqual(last.bandBottom);
		// Beside a wide canvas the card comes to the text, never the reverse; only the phone sheet may move the text above it,
		// and a column opening narrows the text, so there the title stays near where it was (rewrapping moves it a line or two;
		// without the hold it moved over 450 px).
		if (cards === 'popover' || cards === 'margin') for (const step of results) expect(Math.abs(step.textTop - first.textTop), `${step.step}: the clicked title stays put`).toBeLessThanOrEqual(2);
		if (cards === 'opens') for (const step of results) expect(Math.abs(step.textTop - first.textTop), `${step.step}: the clicked title stays near its place`).toBeLessThanOrEqual(48);
	});
}

// The column opening holds the reader's top row, and the editor's own place holder holds it too: with a row cut off at the
// top edge, both take the next row (the first one wholly in view), so the cut-off row growing as the text narrows moves only itself.
test('@surface:live-document the column opening with a row cut off at the top keeps the next row in place at 1440', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name.endsWith('-mobile'), 'The column needs a wide canvas.');
	test.setTimeout(120_000);
	const document = await seedLiveDocument(request, 'table-cut-open-1440', body());
	await createComment(request, document, 'steer2-r2 title:', 'Is this row right?');
	const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
	const results: Array<{ step: string } & Placement> = [];
	try {
		const page = await context.newPage();
		await openLiveDocument(page, document, '# Briefing');
		await widen(page, document as unknown as Doc);
		await page.getByRole('button', { name: 'Close comments' }).click();
		await expect(page.locator('.canvas-comment-drawer.margin')).toHaveCount(0);
		await page.locator('.cm-scroller').hover();
		await page.mouse.wheel(0, 40);
		// steer2-r3 starts 30 px above the scroller's top edge, so steer2-r4 is the first row wholly in view.
		await readAtRow(page, 'steer2-r3', -30);
		results.push({ step: 'before', ...await placement(page, 'steer2-r4') });
		await pill(page).click();
		await expect(page.locator('.canvas-comment-drawer.margin .margin-card').first()).toBeVisible();
		for (const ms of [100, 400, 1_500]) {
			await page.waitForTimeout(ms);
			results.push({ step: `after ${ms} ms`, ...await placement(page, 'steer2-r4') });
		}
	} finally {
		testInfo.annotations.push({ type: 'cut-open', description: JSON.stringify(results) });
		await context.close();
	}
	const first = results[0];
	for (const step of results) expect(Math.abs(step.rowTop - first.rowTop), `${step.step}: the first row wholly in view stays put`).toBeLessThanOrEqual(2);
});
