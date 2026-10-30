import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, openAppByUrl, sendChatMessage, waitForWsConnected } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { releaseMockGate, seedMockResponses } from '../support/mock';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';

/**
 * A reader's place in the chat does not move while an agent's turn is live. A turn that works step by step swaps its
 * live region between reasoning, tool steps and the waiting dots, which are different heights; the text a reader is
 * looking at, read on every painted frame, must not leave its place and come back.
 */

interface Sample { top: number | null; t: number; live: boolean }
type Win = Window & { __eachPaintedFrame?: EachPaintedFrame; __samples?: Sample[]; __done?: boolean };

/** The waiting dots leave when the turn ends, which a reader at the live edge can see as a drop of about this much; it is the only drop allowed, and only on the frames where the live region goes. */
const WAITING_DOTS_PX = 60;
/** While the turn is live the text under a reader at the live edge may only follow new text upward; any drop beyond rounding is the regression. */
const LIVE_DROP_PX = 2;

const LINE = 'The reader keeps their place while the agent works and the reply keeps arriving below. ';

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	await page.getByText(title, { exact: true }).first().click();
}

/** Takes the paragraph under the reading line (`y` px below the list's top) and samples its place on every painted frame for `ms`. */
async function sampleAnchor(page: Page, y: number, ms: number): Promise<Sample[]> {
	await page.evaluate(({ y, ms }) => {
		const win = window as Win;
		const list = document.querySelector<HTMLElement>('.messages')!;
		const box = list.getBoundingClientRect();
		const hit = document.elementFromPoint(box.left + box.width / 2, box.top + y);
		const anchor = hit?.closest('p, li, pre, h1, h2, h3, h4') ?? hit;
		win.__samples = [];
		win.__done = false;
		const start = performance.now();
		win.__eachPaintedFrame!(() => ({
			t: performance.now() - start,
			live: document.querySelector('.live-slot') !== null,
			top: anchor && anchor.isConnected ? anchor.getBoundingClientRect().top - list.getBoundingClientRect().top : null,
		}), (sample) => {
			win.__samples!.push(sample);
			if (sample.t < ms) return true;
			win.__done = true;
			return false;
		});
	}, { y, ms });
	await expect.poll(() => page.evaluate(() => (window as Win).__done), { timeout: 60_000 }).toBe(true);
	return page.evaluate(() => (window as Win).__samples!);
}

/** Frame-to-frame movements of the sampled text, in px; a negative one is the transcript following new text upward. */
function moves(samples: Sample[]): { by: number; at: number; liveBefore: boolean; liveAfter: boolean }[] {
	const out: { by: number; at: number; liveBefore: boolean; liveAfter: boolean }[] = [];
	for (let i = 1; i < samples.length; i++) {
		const [before, after] = [samples[i - 1].top, samples[i].top];
		if (before !== null && after !== null && Math.abs(after - before) >= 0.5) {
			out.push({ by: Math.round((after - before) * 10) / 10, at: Math.round(samples[i].t), liveBefore: samples[i - 1].live, liveAfter: samples[i].live });
		}
	}
	return out;
}

test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

for (const reading of ['scrolled up mid-transcript', 'pinned at the live edge'] as const) {
	test(`@surface:chat-live-scroll the reader's place holds while a turn works (${reading})`, async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop geometry');
		const title = `live-scroll-${Date.now()}`;
		const gate = `hold-live-scroll-${Date.now()}`;
		await setFocusMode(request, false);
		await createSession(request, title);
		await openThread(page, title);
		await waitForWsConnected(page);
		await seedMockResponses(request, [0, 1, 2].map((index) => ({ content: `Reply ${index}. ${LINE.repeat(80)}`, finish_reason: 'stop' })));
		for (let index = 0; index < 3; index++) {
			await sendChatMessage(page, `history message ${index}`);
			await expect(page.locator('.messages .message.assistant').nth(index)).toBeVisible({ timeout: 20_000 });
		}
		await installPaintedFrames(page);
		// Every third step also says something, so the run commits streamed replies mid-turn as well as swapping reasoning and tools.
		const steps = Array.from({ length: 10 }, (_, index) => ({
			reasoning_content: `Looking at step ${index}. `.repeat(20), delay_ms: 500, finish_reason: 'tool_calls',
			...(index % 3 === 2 ? { content: `Step ${index} note. ${LINE.repeat(25)}` } : {}),
			tool_calls: [{ id: `step-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }],
		}));
		await seedMockResponses(request, [...steps, { content: `Live reply. ${LINE.repeat(120)}`, completion_gate: gate, finish_reason: 'stop' }]);
		await sendChatMessage(page, 'start the live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(1500);
		if (reading === 'scrolled up mid-transcript') {
			await page.locator('.messages').evaluate((el) => { el.scrollTop = Math.max(0, el.scrollHeight / 2 - el.clientHeight / 2); });
			await page.waitForTimeout(600);
		}
		// The gate opens part-way through, so the final reply's arrival and the end of the turn are sampled too.
		const sampling = sampleAnchor(page, 200, 12_000);
		await page.waitForTimeout(7000);
		await releaseMockGate(request, gate);
		const samples = await sampling;
		expect(samples.filter((sample) => sample.top !== null).length, 'the sampled text stayed on the page').toBeGreaterThan(samples.length / 2);
		const seen = moves(samples);
		if (reading === 'scrolled up mid-transcript') expect(seen, 'the text under the reader moved while the turn worked').toEqual([]);
		else {
			expect(seen.filter((move) => move.liveBefore && move.liveAfter && move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader at the live edge while the turn worked').toEqual([]);
			expect(seen.filter((move) => move.by > WAITING_DOTS_PX), 'the transcript dropped back under a reader at the live edge when the turn ended').toEqual([]);
		}
	});
}
