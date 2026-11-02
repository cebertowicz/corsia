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

/** The text under a reader at the live edge may only follow new text upward, while the turn is live and when it ends; any drop beyond rounding is the regression. */
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
			live: document.querySelector('button[aria-label="Stop turn"]') !== null,
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

/** With Focus mode on, the live region mounts inside the current-work block, and only once the person opens it. */
for (const reading of ['scrolled up mid-transcript', 'pinned at the live edge', 'pinned at the live edge, current work open (Focus mode)'] as const) {
	test(`@surface:chat-live-scroll the reader's place holds while a turn works (${reading})`, async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop geometry');
		const title = `live-scroll-${Date.now()}`;
		const gate = `hold-live-scroll-${Date.now()}`;
		const focus = reading.includes('Focus mode');
		await setFocusMode(request, focus);
		await createSession(request, title);
		await openThread(page, title);
		await waitForWsConnected(page);
		await seedMockResponses(request, [0, 1, 2].map((index) => ({ content: `Reply ${index}. ${LINE.repeat(80)}`, finish_reason: 'stop' })));
		for (let index = 0; index < 3; index++) {
			// A Focus-mode history is short (replies are tucked away), so the person's own messages carry the length that makes the list scroll.
			await sendChatMessage(page, `history message ${index}. ${focus ? LINE.repeat(40) : ''}`);
			// With Focus mode on, a reply not meant for the person is tucked into a Worked block rather than shown.
			if (focus) await expect(page.getByRole('button', { name: /^Expand Worked/ })).toHaveCount(index + 1, { timeout: 20_000 });
			else await expect(page.locator('.messages .message.assistant').nth(index)).toBeVisible({ timeout: 20_000 });
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
		if (focus) {
			await page.getByRole('button', { name: /^Expand current work/ }).click();
			await expect(page.getByRole('button', { name: 'Stop turn' })).toBeVisible();
		}
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
			expect(seen.filter((move) => move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader at the live edge when the turn ended').toEqual([]);
		}
	});
}

/**
 * Where a turn ends, and after a step of many tool calls, the reader at the live edge is not moved either: the live
 * row's tool list scrolls inside the height the reasoning box has, and what the live row loses when it goes is held as
 * blank space under the transcript until the person sends again.
 */
const LIST_CAP_PX = 150;
const widths = [{ name: '1440', width: 1440, height: 900 }, { name: '393 touch', width: 393, height: 852 }] as const;

async function openEndThread(page: Page, request: Parameters<typeof createSession>[0], size: (typeof widths)[number], otherTitle?: string): Promise<string> {
	await setFocusMode(request, false);
	await page.setViewportSize({ width: size.width, height: size.height });
	const title = `live-end-${Date.now()}`;
	await createSession(request, title);
	if (otherTitle) await createSession(request, otherTitle);
	await openAppByUrl(page);
	if (size.width < 700) { await page.waitForTimeout(2500); await page.getByRole('button', { name: 'Toggle sidebar' }).click(); }
	await page.getByText(title, { exact: true }).first().click();
	await waitForWsConnected(page);
	await seedMockResponses(request, [0, 1, 2].map((index) => ({ content: `Reply ${index}. ${LINE.repeat(80)}`, finish_reason: 'stop' })));
	for (let index = 0; index < 3; index++) {
		await sendChatMessage(page, `history message ${index}.`);
		await expect(page.locator('.messages .message.assistant').nth(index)).toBeVisible({ timeout: 20_000 });
	}
	await installPaintedFrames(page);
	return title;
}

/** The blank space the floor holds: how far the floor marker's edge sits below where the content actually ends. */
function readHeld(): number {
	const marker = document.querySelector<HTMLElement>('.tail-hold');
	const end = document.querySelector<HTMLElement>('.tail-end');
	if (!marker || !end || marker.offsetHeight === 0) return 0;
	const list = marker.parentElement!;
	return Math.max(0, Math.round(marker.offsetTop + marker.offsetHeight - (end.offsetTop + parseFloat(getComputedStyle(list).paddingBottom))));
}
const heldSpace = (page: Page) => page.evaluate(readHeld);

for (const size of widths) {
	for (const ending of ['a short reply', 'Stop'] as const) {
		test(`@surface:chat-live-scroll the reader's place holds when a turn ends with ${ending} (${size.name})`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
			await openEndThread(page, request, size);
			const gate = `hold-end-${Date.now()}`;
			const steps = Array.from({ length: 3 }, (_, index) => ({
				reasoning_content: `Looking at step ${index}. `.repeat(20), delay_ms: 500, finish_reason: 'tool_calls',
				tool_calls: [{ id: `step-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }],
			}));
			const last = ending === 'Stop'
				? { content: 'Done.', response_gate: gate, finish_reason: 'stop' }
				: { reasoning_content: 'Wrapping up. '.repeat(20), content: 'Done.', completion_gate: gate, finish_reason: 'stop' };
			await seedMockResponses(request, [...steps, last]);
			await sendChatMessage(page, 'start the live turn');
			await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
			await page.waitForTimeout(4500);
			const sampling = sampleAnchor(page, 200, 3500);
			await page.waitForTimeout(600);
			if (ending === 'Stop') await page.getByRole('button', { name: 'Stop turn' }).click();
			else await releaseMockGate(request, gate);
			const samples = await sampling;
			expect(samples.filter((sample) => sample.top !== null).length, 'the sampled text stayed on the page').toBeGreaterThan(samples.length / 2);
			expect(moves(samples).filter((move) => move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader at the live edge when the turn ended').toEqual([]);
		});
	}

	test(`@surface:chat-live-scroll many live tool calls scroll inside the list and leave no tall blank band (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		await openEndThread(page, request, size);
		const gate = `hold-many-${Date.now()}`;
		const many = Array.from({ length: 30 }, (_, index) => ({ id: `many-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
		await seedMockResponses(request, [
			{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
			{ content: 'Done.', response_gate: gate, finish_reason: 'stop' },
		]);
		await sendChatMessage(page, 'start the live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		// The send's own scroll is not what is measured, so sampling starts 400ms after it. In the first 300ms at 393 the list moves 16px
		// with its height unchanged, on dev as well (8 of 8 runs sampled from the send, 4 of them exactly 16px); it is not the live row.
		await page.waitForTimeout(400);
		const sampling = sampleAnchor(page, 200, 6000);
		let tallest = 0;
		let heldMost = 0;
		const started = Date.now();
		while (Date.now() - started < 5000) {
			const read = {
				list: await page.evaluate(() => document.querySelector<HTMLElement>('.ta-rows')?.getBoundingClientRect().height ?? 0),
				held: await heldSpace(page),
			};
			tallest = Math.max(tallest, read.list);
			heldMost = Math.max(heldMost, read.held);
		}
		expect(tallest, 'the live tool list ran past the height the reasoning box has').toBeLessThanOrEqual(LIST_CAP_PX + 10);
		expect(heldMost, 'the held space is taller than the live row can lose').toBeLessThanOrEqual(LIST_CAP_PX + 60);
		expect(moves(await sampling).filter((move) => move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader at the live edge').toEqual([]);
		await releaseMockGate(request, gate);
	});

	test(`@surface:chat-live-scroll the held space carries into the next send without moving the reader (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(150_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		await openEndThread(page, request, size);
		const gate = `hold-next-${Date.now()}`;
		const many = Array.from({ length: 30 }, (_, index) => ({ id: `next-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
		await seedMockResponses(request, [
			{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
			{ content: 'Done.', response_gate: gate, finish_reason: 'stop' },
		]);
		await sendChatMessage(page, 'first live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(3500);
		await releaseMockGate(request, gate);
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30_000 });
		await page.waitForTimeout(800);
		expect(await heldSpace(page), 'the space the live row lost is held after the turn ends').toBeGreaterThan(50);
		await seedMockResponses(request, [
			{ reasoning_content: 'Next. '.repeat(20), delay_ms: 400, finish_reason: 'tool_calls', tool_calls: [{ id: `next-one-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }] },
			{ content: `Again. ${LINE.repeat(6)}`, finish_reason: 'stop' },
		]);
		const sampling = sampleAnchor(page, 200, 3500);
		await sendChatMessage(page, 'second turn');
		const seen = moves(await sampling);
		expect(seen.filter((move) => move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader when the next send began').toEqual([]);
	});

	test(`@surface:chat-live-scroll expanding and collapsing a step after the turn ends leaves no more blank space (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(150_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		await openEndThread(page, request, size);
		const gate = `hold-toggle-${Date.now()}`;
		const many = Array.from({ length: 30 }, (_, index) => ({ id: `toggle-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
		await seedMockResponses(request, [
			{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
			{ content: 'Done.', response_gate: gate, finish_reason: 'stop' },
		]);
		await sendChatMessage(page, 'start the live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(3500);
		await releaseMockGate(request, gate);
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30_000 });
		await page.waitForTimeout(800);
		const afterTurn = await heldSpace(page);
		expect(afterTurn, 'the space the live row lost is held after the turn ends').toBeGreaterThan(50);
		const summary = page.locator('.messages .ta-summary').last();
		await summary.click();
		await page.waitForTimeout(500);
		await summary.click();
		await page.waitForTimeout(500);
		expect(await heldSpace(page), 'collapsing a step after the turn left more blank space than the turn did').toBeLessThanOrEqual(afterTurn + 2);
	});

	test(`@surface:chat-live-scroll older history loaded after a turn ends moves no text (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(150_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		// The thread says it has older history; the page of it is held until the reader is at the top, after the turn.
		let releaseOlder!: () => void;
		const olderGate = new Promise<void>((resolve) => { releaseOlder = resolve; });
		let olderAsked = false;
		await page.route('**/api/threads/*/messages**', async (route) => {
			const url = route.request().url();
			if (!url.includes('before=')) {
				const response = await route.fetch();
				const body = await response.json();
				await route.fulfill({ response, json: { ...body, has_more: true, oldest_id: body.oldest_id ?? 'older-cursor' } });
				return;
			}
			olderAsked = true;
			await olderGate;
			const threadId = /\/api\/threads\/([^/]+)\/messages/.exec(url)![1];
			const messages = Array.from({ length: 8 }, (_, index) => ({
				id: `older-${index}`, thread_id: threadId, role: index % 2 === 0 ? 'user' : 'assistant',
				content: `Older message ${index}. ${LINE.repeat(10)}`, created_at: `2026-06-12T00:00:00.00000${index}`, metadata: null,
				// Sequence 1 is the lowest a row can have; rows with the same one are ordered by time, so these come first.
				seq: 1,
			}));
			await route.fulfill({ json: { messages, has_more: true, oldest_id: messages[0].id } });
		});
		try {
			await openEndThread(page, request, size);
			const gate = `hold-older-${Date.now()}`;
			const many = Array.from({ length: 30 }, (_, index) => ({ id: `older-call-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
			await seedMockResponses(request, [
				{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
				{ content: 'Done.', response_gate: gate, finish_reason: 'stop' },
			]);
			await sendChatMessage(page, 'start the live turn');
			await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
			await page.waitForTimeout(3500);
			await releaseMockGate(request, gate);
			await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30_000 });
			await page.waitForTimeout(800);
			const heldBefore = await heldSpace(page);
			expect(heldBefore, 'the space the live row lost is held after the turn ends').toBeGreaterThan(30);
			await page.locator('.messages').evaluate((el) => { el.scrollTop = 0; });
			await expect.poll(() => olderAsked, { message: 'reaching the top asks for older history' }).toBe(true);
			const sampling = sampleAnchor(page, 200, 2500);
			await page.waitForTimeout(300);
			// The row at the top that says it is loading is replaced by the one that offers more; a browser without scroll anchoring
			// moves the text by the difference in their heights, which is not what is measured here.
			const rowHeight = () => page.locator('.loading-older').first().evaluate((el) => el.getBoundingClientRect().height).catch(() => 0);
			const loadingRow = await rowHeight();
			releaseOlder();
			const seen = moves(await sampling);
			await expect(page.getByText('Older message 0.', { exact: false })).toHaveCount(1);
			const rowChange = Math.abs((await rowHeight()) - loadingRow);
			expect(seen.filter((move) => Math.abs(move.by) > rowChange + LIVE_DROP_PX), 'the text under the reader moved when older history loaded').toEqual([]);
		} finally {
			releaseOlder();
		}
	});

	test(`@surface:chat-live-scroll jump to latest ignores the held space (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(150_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		await openEndThread(page, request, size);
		const gate = `hold-jump-${Date.now()}`;
		const many = Array.from({ length: 30 }, (_, index) => ({ id: `jump-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
		await seedMockResponses(request, [
			{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
			{ content: 'Done.', completion_gate: gate, finish_reason: 'stop' },
		]);
		await sendChatMessage(page, 'start the live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(3500);
		const list = page.locator('.messages');
		const jump = page.getByRole('button', { name: 'Scroll to bottom' });
		/** Px of content below the view, measured to where the content ends, not to the held space. */
		const belowView = async () => (await list.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight)) - (await heldSpace(page));
		expect(await heldSpace(page), 'the held space is armed mid-turn').toBeGreaterThan(50);
		// Mid-turn: scroll up, tap the control, and keep reading as the reply arrives and the turn ends.
		await list.evaluate((el) => { el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 700); });
		await expect(jump, 'the control shows once the reader is scrolled up').toBeVisible();
		await jump.click();
		await expect(jump, 'the control goes once the reader is back at the latest').toHaveCount(0);
		// Live content may still shrink (a step's tools folding into a row), which leaves blank below, never content hidden below.
		expect(await belowView(), 'the tap lands with the latest content in view').toBeLessThanOrEqual(2);
		const sampling = sampleAnchor(page, 200, 3000);
		await releaseMockGate(request, gate);
		expect(moves(await sampling).filter((move) => move.by > LIVE_DROP_PX), 'the transcript dropped back under a reader after the tap').toEqual([]);
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30_000 });
		await page.waitForTimeout(800);
		expect(await heldSpace(page), 'the space the live row lost is still held').toBeGreaterThan(50);
		// After the turn: a reader whose view ends at the last message sees no control; scrolled up, the tap lands as on a thread with no held space.
		const held = await heldSpace(page);
		await list.evaluate((el, floor) => { el.scrollTop = el.scrollHeight - floor - el.clientHeight; }, held);
		await expect(jump, 'no control while the reader is at the last message with only the held space below').toHaveCount(0);
		await list.evaluate((el) => { el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight - 700); });
		await expect(jump).toBeVisible();
		await jump.click();
		await expect(jump).toHaveCount(0);
		expect(Math.abs(await belowView()), 'the tap after the turn lands with the last message at the bottom of the view').toBeLessThanOrEqual(2);
	});

	test(`@surface:chat-live-scroll leaving the thread releases the held space (${size.name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(150_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'desktop project drives the viewport');
		const other = `live-other-${Date.now()}`;
		const title = await openEndThread(page, request, size, other);
		const gate = `hold-leave-${Date.now()}`;
		const many = Array.from({ length: 30 }, (_, index) => ({ id: `leave-${index}-${gate}`, name: 'list_dir', arguments: JSON.stringify({ path: '.' }) }));
		await seedMockResponses(request, [
			{ reasoning_content: 'Checking several places at once. '.repeat(20), delay_ms: 500, finish_reason: 'tool_calls', tool_calls: many },
			{ content: 'Done.', response_gate: gate, finish_reason: 'stop' },
		]);
		await sendChatMessage(page, 'first live turn');
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(3500);
		await releaseMockGate(request, gate);
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0, { timeout: 30_000 });
		await page.waitForTimeout(800);
		expect(await heldSpace(page), 'the space the live row lost is held after the turn ends').toBeGreaterThan(50);
		for (const next of [other, title]) {
			if (size.width < 700) { await page.waitForTimeout(600); await page.getByRole('button', { name: 'Toggle sidebar' }).click(); }
			await page.getByText(next, { exact: true }).first().click();
			await page.waitForTimeout(800);
		}
		expect(await heldSpace(page), 'the held space stayed after the person left and came back').toBe(0);
		const gap = await page.locator('.messages').evaluate((el) => Math.round(el.scrollHeight - el.clientHeight - el.scrollTop));
		expect(gap, 'the chat is at its end after coming back').toBeLessThanOrEqual(1);
	});
}
