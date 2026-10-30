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

interface Sample { top: number | null; t: number }
type Win = Window & { __eachPaintedFrame?: EachPaintedFrame; __samples?: Sample[]; __done?: boolean };

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
function moves(samples: Sample[]): number[] {
	const out: number[] = [];
	for (let i = 1; i < samples.length; i++) {
		const [before, after] = [samples[i - 1].top, samples[i].top];
		if (before !== null && after !== null && Math.abs(after - before) >= 0.5) out.push(Math.round((after - before) * 10) / 10);
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
		const steps = Array.from({ length: 10 }, (_, index) => ({
			reasoning_content: `Looking at step ${index}. `.repeat(20), delay_ms: 500, finish_reason: 'tool_calls',
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
		const samples = await sampleAnchor(page, 200, 8000);
		await releaseMockGate(request, gate);
		expect(samples.filter((sample) => sample.top !== null).length, 'the sampled text stayed on the page').toBeGreaterThan(samples.length / 2);
		const seen = moves(samples);
		if (reading === 'scrolled up mid-transcript') expect(seen, 'the text under the reader moved while the turn worked').toEqual([]);
		else expect(seen.filter((by) => by > 0), 'the transcript dropped back under a reader at the live edge').toEqual([]);
	});
}
