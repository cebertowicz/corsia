import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { openLiveDocument, seedLiveDocument, serverText, type LiveDocument } from '../support/liveDocument';
import { layouts } from '../support/liveConcurrency';

/**
 * Typing in a document never moves it. Each stretch of typing is saved a moment after it stops, and while that write
 * is unsettled the live session reports it; the "still settling" notice used to mount in the header for that time
 * and push the whole document down and back. The notice now shows only once a write has stayed unsettled for 2 s.
 * The proof is the position of the first paragraph, the table and the header, sampled in the page every 50 ms
 * through the typing, the save after it and the settling, and again across a write held for less than the wait.
 * A write that is truly stuck still shows the notice, and the text moves once.
 */

const INTRO = 'Intro line above the table.';
const CELL = 'Trailhead signs';
const BODY = ['# Typing never moves this', '', INTRO, '', '| Item | Notes |', '| --- | --- |', `| ${CELL} | Replace the faded sign |`, '', 'Paragraph below the table.', ''].join('\n');
const RENDERED = ['# Typing never moves this', INTRO, 'Paragraph below the table.'].join('\n');
/** The most a run of typing may take, so the sampled window always holds all of it. */
const TYPING_MS = 2500;
/** Long enough after the last keystroke for the save's 700 ms wait, the write itself and its settling. */
const AFTER_TYPING_MS = 2500;
/** The wait the status component allows an unsettled write before it says so; the held-write case runs past it. */
const NOTICE_AFTER_MS = 2000;
/** How long the held-write case watches before the wait ends, leaving room for a slow screenshot or a loaded host. */
const WATCH_MS = NOTICE_AFTER_MS - 1000;

type Sample = { paragraph: number; table: number; header: number };

/** Where the reader's eye finds the top of the first paragraph and of the table, and how tall the header is, in page pixels. */
function sample(page: Page, windowMs: number): Promise<Sample[]> {
	return page.evaluate(({ intro, windowMs }) => new Promise<Sample[]>((resolve) => {
		const samples: Sample[] = [];
		const started = performance.now();
		const read = () => {
			const line = [...document.querySelectorAll('.cm-line')].find((node) => node.textContent?.startsWith(intro));
			samples.push({
				paragraph: line?.getBoundingClientRect().top ?? Number.NaN,
				table: document.querySelector('.cm-md-table table')?.getBoundingClientRect().top ?? Number.NaN,
				header: document.querySelector('.document-header')?.getBoundingClientRect().height ?? Number.NaN,
			});
			if (performance.now() - started >= windowMs) { clearInterval(timer); resolve(samples); }
		};
		const timer = setInterval(read, 50);
		read();
	}), { intro: INTRO, windowMs });
}

function expectStill(samples: Sample[], what: string, minSamples = 30) {
	expect(samples.length, `${what}: the sampler ran`).toBeGreaterThan(minSamples);
	for (const key of ['paragraph', 'table', 'header'] as const) {
		const values = samples.map((item) => item[key]);
		expect(values.every(Number.isFinite), `${what}: the ${key} was found in every sample`).toBe(true);
		expect(Math.max(...values) - Math.min(...values), `${what}: the ${key} moved (${[...new Set(values.map((value) => Math.round(value)))].join(' -> ')} px)`).toBe(0);
	}
}

/** Counts the frames the server sends while a write is unsettled, so a run proves the typing reached that state. Install before the page opens. */
function watchUnsettled(page: Page): { count(): number } {
	let count = 0;
	page.on('websocket', (socket) => socket.on('framereceived', (frame) => {
		try { if (JSON.parse(String(frame.payload)).type === 'doc_pending' && JSON.parse(String(frame.payload)).pending === true) count += 1; } catch { /* not a JSON frame */ }
	}));
	return { count: () => count };
}

async function frame(page: Page, name: string) {
	const dir = process.env.CORSIA_SETTLE_FRAMES;
	if (!dir) return;
	mkdirSync(dir, { recursive: true });
	await page.screenshot({ path: path.join(dir, `${name}.png`), animations: 'disabled' });
}

async function open(page: Page, request: Parameters<typeof seedLiveDocument>[0], width: number): Promise<LiveDocument> {
	const document = await seedLiveDocument(request, `no-shift-${width}`, BODY);
	// The table renders as a widget, so its pipe-delimited source lines are not in the editor's text.
	await openLiveDocument(page, document, RENDERED);
	await expect(page.locator('.cm-md-table table')).toBeVisible();
	// The editor at rest: nothing animating before the first sample.
	await expect.poll(() => page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().iterations !== Infinity).length)).toBe(0);
	return document;
}

/** Samples through `type` and the save that follows it. */
async function typeAndSample(page: Page, type: () => Promise<void>): Promise<Sample[]> {
	const started = Date.now();
	const sampling = sample(page, TYPING_MS + AFTER_TYPING_MS);
	await type();
	expect(Date.now() - started, 'setup: the typing finished inside the sampled window').toBeLessThan(TYPING_MS);
	return sampling;
}

for (const [width, height, project] of layouts) {
	const phone = width <= 768;
	test.describe(`@flow:doc-typing-no-shift @surface:documents ${width}`, () => {
		test.beforeEach(({}, testInfo) => { test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.'); });

		test(`typing in a paragraph moves neither the paragraph nor the table at ${width}`, async ({ page, request }) => {
			test.setTimeout(120_000);
			await page.setViewportSize({ width, height });
			const unsettled = watchUnsettled(page);
			const document = await open(page, request, width);
			await page.locator('.cm-line', { hasText: INTRO }).click({ position: { x: 8, y: 6 } });
			await page.keyboard.press('End');
			expectStill(await typeAndSample(page, () => page.keyboard.type(' typed here', { delay: 40 })), 'typing in a paragraph');
			await expect.poll(() => serverText(request, document), { message: 'the typed text was saved' }).toContain(`${INTRO} typed here`);
			expect(unsettled.count(), 'the save reported an unsettled write while the document was sampled').toBeGreaterThan(0);
		});

		// Enabled once #2462's cell editor merges: until then a press or tap in a rendered cell places no caret.
		test.fixme(`typing in a table cell moves neither the paragraph nor the table at ${width}`, async ({ page, request }) => {
			test.setTimeout(120_000);
			await page.setViewportSize({ width, height });
			const unsettled = watchUnsettled(page);
			const document = await open(page, request, width);
			const cell = page.locator('.cm-md-table td', { hasText: CELL });
			if (phone) await cell.tap(); else await cell.click();
			expectStill(await typeAndSample(page, () => page.keyboard.type('QQQ', { delay: 40 })), 'typing in a table cell');
			await expect.poll(() => serverText(request, document), { message: 'the typed text was saved' }).toContain('QQQ');
			expect(unsettled.count(), 'the save reported an unsettled write while the document was sampled').toBeGreaterThan(0);
		});

		test(`a write unsettled for less than the wait moves nothing, and one held past it shows the notice at ${width}`, async ({ page, request }) => {
			test.setTimeout(120_000);
			await page.setViewportSize({ width, height });
			await open(page, request, width);
			const notice = page.getByRole('status').filter({ hasText: 'This write is still settling' });
			await expect(notice).toHaveCount(0);
			await frame(page, `${width}-1-at-rest`);
			const sampling = sample(page, WATCH_MS);
			// The frame the server sends while a write is unsettled, delivered to the open session and never settled.
			await page.evaluate(async () => {
				const { liveDocumentBindings } = await import('/src/lib/stores/liveDocuments.ts');
				let bindings: Record<string, { artifactId: string; storeId: string; handle(frame: Record<string, unknown>): void }> = {};
				liveDocumentBindings.subscribe((value) => { bindings = value as typeof bindings; })();
				const [session] = Object.values(bindings);
				session.handle({ type: 'doc_pending', artifact_id: session.artifactId, store_id: session.storeId, pending: true, held: null });
			});
			await page.waitForTimeout(WATCH_MS);
			await expect(notice, 'the notice waits out a write that has not stayed unsettled').toHaveCount(0);
			await frame(page, `${width}-2-before-the-wait-ends`);
			// Sampled only up to here: a write that is truly stuck still shows the notice in the header, which moves the text once.
			expectStill(await sampling, 'a write unsettled for less than the wait', 8);
			await expect(notice, 'the notice shows once the write has stayed unsettled').toBeVisible({ timeout: 3000 });
			await frame(page, `${width}-3-notice-shown`);
		});
	});
}
