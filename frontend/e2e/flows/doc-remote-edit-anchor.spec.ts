import fs from 'node:fs';
import path from 'node:path';
import { expect, type APIRequestContext, type Browser, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage } from '../support/corsia';
import { test } from '../support/test';
import { openLiveDocument, seedLiveDocument, type LiveDocument } from '../support/liveDocument';
import { readMockCallCount, seedMockResponses } from '../support/mock';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { installPaintedFrames, type EachPaintedFrame } from '../support/paintedFrames';
import { syntheticPng } from '../support/syntheticImages';

/**
 * Another writer's edits never move the reader's view: the text at the top of the reading pane stays where it is on
 * screen while text is inserted, deleted, replaced or typed above, inside or below it. Each edit is a real remote
 * update the reader applies from the server's broadcast: a second browser editing the same live document, or an agent
 * editing it in a mock-provider turn through the live lane.
 */

const WORDS = 'the quick reader keeps their place while another writer changes the document around the text in view'.split(' ');
const paragraph = (n: number, words = 70) => Array.from({ length: words }, (_, i) => WORDS[(i + n) % WORDS.length]).join(' ') + ` (para ${n}).`;
const TABLE = ['| Column A | Column B | Column C |', '| --- | --- | --- |', ...Array.from({ length: 6 }, (_, i) => `| row ${i} a | row ${i} b | row ${i} c |`)].join('\n');

function body(): string {
	const out = ['# Anchor document', ''];
	for (let n = 1; n <= 60; n++) {
		out.push(`## Section ${n}`, '', paragraph(n), '');
		if (n === 10) out.push(TABLE, '');
		if (n === 20) out.push('![Anchor shot](shots/anchor.png)', '');
	}
	return out.join('\n');
}

interface Sample { t: number; top: number | null; scrollTop: number }
interface Drift { name: string; maxDrift: number; finalDrift: number; scrollTopDelta: number; frames: number; missing: number }

type Win = Window & { __eachPaintedFrame?: EachPaintedFrame; __anchorSamples?: Sample[]; __anchorDone?: boolean };

/** The reading pane's editor; its view class is the one that built it. */
const editorView = `(() => { const content = document.querySelector('.cm-content'); return content && content.cmTile && content.cmTile.root.view; })()`;

/** Scrolls the reader so `heading` sits `margin` px below the pane's top, as a reader would leave it. */
async function readAt(page: Page, heading: string, margin = 40): Promise<void> {
	await page.evaluate(({ editorView, heading, margin }) => {
		const view = eval(editorView);
		const at = view.state.doc.toString().indexOf(heading);
		view.dispatch({ effects: view.constructor.scrollIntoView(at, { y: 'start', yMargin: margin }) });
	}, { editorView, heading, margin });
	await page.waitForTimeout(600);
}

/** The first line whose top is inside the pane: its text and its distance below the pane's top. */
async function anchorLine(page: Page): Promise<{ text: string; top: number }> {
	return page.evaluate(() => {
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const box = scroller.getBoundingClientRect();
		const line = [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((el) => el.getBoundingClientRect().top >= box.top && (el.textContent ?? '').trim().length > 12);
		if (!line) throw new Error('no line in view');
		return { text: (line.textContent ?? '').trim(), top: line.getBoundingClientRect().top - box.top };
	});
}

/** Samples the anchor line's distance on every painted frame for `ms`. */
async function startSampling(page: Page, text: string, ms: number): Promise<void> {
	await page.evaluate(({ text, ms }) => {
		const win = window as Win;
		win.__anchorSamples = [];
		win.__anchorDone = false;
		const start = performance.now();
		win.__eachPaintedFrame!(() => {
			const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
			const box = scroller.getBoundingClientRect();
			const line = [...scroller.querySelectorAll<HTMLElement>('.cm-line')].find((el) => (el.textContent ?? '').trim() === text);
			return { t: performance.now() - start, top: line ? line.getBoundingClientRect().top - box.top : null, scrollTop: scroller.scrollTop };
		}, (sample) => {
			win.__anchorSamples!.push(sample);
			if (sample.t < ms) return true;
			win.__anchorDone = true;
			return false;
		});
	}, { text, ms });
}

async function drift(page: Page, name: string, anchor: { top: number }): Promise<Drift> {
	await expect.poll(() => page.evaluate(() => (window as Win).__anchorDone), { timeout: 30_000 }).toBe(true);
	const samples = await page.evaluate(() => (window as Win).__anchorSamples!);
	const seen = samples.filter((s) => s.top !== null);
	const deltas = seen.map((s) => s.top! - anchor.top);
	return {
		name,
		maxDrift: Math.round(Math.max(0, ...deltas.map(Math.abs)) * 10) / 10,
		finalDrift: Math.round((deltas.at(-1) ?? NaN) * 10) / 10,
		scrollTopDelta: Math.round((samples.at(-1)!.scrollTop - samples[0].scrollTop) * 10) / 10,
		frames: samples.length,
		missing: samples.length - seen.length,
	};
}

/** Applies `edit(doc)` in the writer's editor as one or more transactions; the reader receives each as a remote update. */
async function writerEdits(writer: Page, edit: { find: string; offset?: number; remove?: number; insert?: string; typed?: boolean }): Promise<void> {
	await writer.evaluate(async ({ editorView, edit }) => {
		const view = eval(editorView);
		const at = view.state.doc.toString().indexOf(edit.find);
		if (at < 0) throw new Error(`writer cannot find ${edit.find}`);
		const from = at + (edit.offset ?? 0);
		if (edit.typed) {
			for (let i = 0; i < (edit.insert ?? '').length; i++) {
				view.dispatch({ changes: { from: from + i, insert: edit.insert![i] } });
				await new Promise((resolve) => setTimeout(resolve, 25));
			}
			return;
		}
		view.dispatch({ changes: { from, to: from + (edit.remove ?? 0), insert: edit.insert ?? '' } });
	}, { editorView, edit });
}

const BLOCK = (n: number) => Array.from({ length: n }, (_, i) => `Inserted paragraph ${i}: ${paragraph(100 + i, 40)}\n\n`).join('');

const cases: Array<{ name: string; edit: Parameters<typeof writerEdits>[1] }> = [
	{ name: 'insert above', edit: { find: '## Section 5\n', insert: BLOCK(2) } },
	{ name: 'delete above', edit: { find: '## Section 6\n', remove: `## Section 6\n\n${paragraph(6)}\n\n`.length } },
	{ name: 'replace above', edit: { find: paragraph(8), remove: paragraph(8).length, insert: 'A much shorter paragraph eight.' } },
	{ name: 'large block above', edit: { find: '## Section 3\n', insert: BLOCK(40) } },
	{ name: 'typing in view', edit: { find: '## Section 31\n', offset: '## Section 31\n\n'.length, insert: 'Typed words arriving inside the section in view, one at a time. ', typed: true } },
	{ name: 'insert in view', edit: { find: '## Section 31\n', offset: '## Section 31\n\n'.length, insert: BLOCK(3) } },
	{ name: 'insert below', edit: { find: '## Section 50\n', insert: BLOCK(5) } },
];

async function pair(browser: Browser, document: LiveDocument, width: number, height: number) {
	const writerContext = await browser.newContext({ viewport: { width, height } });
	const readerContext = await browser.newContext({ viewport: { width, height } });
	const writer = await writerContext.newPage();
	const reader = await readerContext.newPage();
	await openLiveDocument(writer, document, '# Anchor document');
	await openLiveDocument(reader, document, '# Anchor document');
	return { writer, reader, close: async () => { await writerContext.close(); await readerContext.close(); } };
}

/** Leaves the reader a measured distance from the live edge after CodeMirror has settled its scroll height. */
async function readNearEnd(page: Page, remaining: number): Promise<void> {
	const scroller = page.locator('.cm-scroller');
	await scroller.evaluate((element) => { element.scrollTop = element.scrollHeight; });
	await expect.poll(() => scroller.evaluate((element) =>
		Math.round(element.scrollHeight - element.scrollTop - element.clientHeight),
	)).toBeLessThanOrEqual(2);
	await page.waitForTimeout(500);
	await scroller.evaluate((element, distance) => { element.scrollTop -= distance; }, remaining);
	await expect.poll(() => scroller.evaluate((element) =>
		Math.round(element.scrollHeight - element.scrollTop - element.clientHeight),
	)).toBe(remaining);
}

/** Appends a remote change in the writer's editor, so the reader receives the real collaboration broadcast. */
async function append(writer: Page, text: string): Promise<void> {
	await writer.evaluate(({ editorView, text }) => {
		const view = eval(editorView);
		view.dispatch({ changes: { from: view.state.doc.length, insert: text } });
	}, { editorView, text });
}

const LAYOUTS = [[1440, 900], [393, 852]] as const;

function agentBody(): string {
	return body()
		.replace(`${paragraph(5)}\n`, `${paragraph(5)}\n\nMARKER-ABOVE\n`)
		.replace(`## Section 31\n\n`, `## Section 31\n\nMARKER-VIEW `)
		.replace(`${paragraph(50)}\n`, `${paragraph(50)}\n\nMARKER-BELOW\n`);
}

interface Agent { id: string; title: string; page: Page }

/**
 * The agent's chat runs in its own browser, so the reader's page stays the one in front. It shares the reader's layout:
 * a mobile project's touch composer sends only from its button, which the chat helpers use at a narrow viewport.
 */
async function openAgent(request: APIRequestContext, browser: Browser, title: string, width: number, height: number): Promise<Agent> {
	const id = await createSession(request, title);
	const page = await (await browser.newContext({ viewport: { width, height } })).newPage();
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	return { id, title, page };
}

/** The agent reads the document, then replaces `marker` with `text` in a real mock-provider turn through the live lane. */
async function agentWrites(request: APIRequestContext, agent: Agent, live: LiveDocument, marker: string, text: string): Promise<void> {
	const stamp = `${agent.id}-${Date.now()}`;
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ tool_calls: [{ id: `edit-${stamp}`, name: 'edit_file', arguments: JSON.stringify({ path: live.docPath, old_string: marker, new_string: text }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ content: `Edited by ${agent.title}.`, finish_reason: 'stop', match_thread: agent.id },
	]);
	await sendChatMessage(agent.page, 'Edit the document.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
}

const agentCases: Array<{ name: string; marker: string; text: string }> = [
	{ name: 'agent writes above', marker: 'MARKER-ABOVE', text: Array.from({ length: 6 }, (_, i) => paragraph(200 + i, 60)).join('\n\n') },
	{ name: 'agent writes in view', marker: 'MARKER-VIEW ', text: `${paragraph(300, 80)}\n\n${paragraph(301, 80)}\n\n` },
	{ name: 'agent shortens above', marker: paragraph(8), text: 'Paragraph eight, now short.' },
	{ name: 'agent writes below', marker: 'MARKER-BELOW', text: Array.from({ length: 4 }, (_, i) => paragraph(400 + i, 60)).join('\n\n') },
];

for (const [width, height] of LAYOUTS) {
	test(`@surface:live-document an agent's live writes leave the reader's place at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip((width <= 768) !== (testInfo.project.name === 'chromium-mobile'), 'One layout per project.');
		test.setTimeout(240_000);
		const context = await browser.newContext({ viewport: { width, height } });
		const results: Drift[] = [];
		let agentId = '';
		let agentPage: Page | undefined;
		try {
			const reader = await context.newPage();
			const agent = await openAgent(request, browser, `Anchor Agent ${width}-${Date.now()}`, width, height);
			agentId = agent.id;
			agentPage = agent.page;
			const live = await agentCreatesSharedDocument(request, agent, `anchor-agent-${width}`, agentBody());
			fs.mkdirSync(path.join(path.dirname(live.docPath), 'shots'), { recursive: true });
			fs.writeFileSync(path.join(path.dirname(live.docPath), 'shots', 'anchor.png'), syntheticPng(640, 400));
			await openLiveDocument(reader, live, '# Anchor document');
			await installPaintedFrames(reader);
			for (const { name, marker, text } of agentCases) {
				await readAt(reader, '## Section 30');
				const anchor = await anchorLine(reader);
				await startSampling(reader, anchor.text, 16_000);
				await reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-${name.replace(/ /g, '-')}-before.png`) });
				await agentWrites(request, agent, live, marker, text);
				await reader.waitForTimeout(1_500);
				await reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-${name.replace(/ /g, '-')}-typing.png`) });
				const result = await drift(reader, name, anchor);
				await reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-${name.replace(/ /g, '-')}-after.png`) });
				results.push(result);
			}
		} finally {
			fs.writeFileSync(testInfo.outputPath(`anchor-agent-drift-${testInfo.project.name}-${width}.json`), JSON.stringify(results, null, 2));
			testInfo.annotations.push({ type: 'drift', description: JSON.stringify(results) });
			if (agentId) await demoteAgent(request, agentId);
			await agentPage?.context().close();
			await context.close();
		}
		for (const result of results) {
			expect.soft(result.maxDrift, `${result.name}: the text in view never moves`).toBeLessThanOrEqual(3);
		}
	});
}

for (const [width, height] of LAYOUTS) {
	test(`@surface:live-document another writer's edits leave the reader's place at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip((width <= 768) !== (testInfo.project.name === 'chromium-mobile'), 'One layout per project.');
		test.setTimeout(180_000);
		const text = body();
		const document = await seedLiveDocument(request, `anchor-${width}`, text);
		fs.mkdirSync(path.join(path.dirname(document.docPath), 'shots'), { recursive: true });
		fs.writeFileSync(path.join(path.dirname(document.docPath), 'shots', 'anchor.png'), syntheticPng(640, 400));
		const live = await pair(browser, document, width, height);
		const results: Drift[] = [];
		try {
			await installPaintedFrames(live.reader);
			await live.reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-opened.png`) });
			for (const { name, edit } of cases) {
				await readAt(live.reader, '## Section 30');
				const anchor = await anchorLine(live.reader);
				await startSampling(live.reader, anchor.text, 2_500);
				await live.reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-${name.replace(/ /g, '-')}-before.png`) });
				await writerEdits(live.writer, edit);
				const result = await drift(live.reader, name, anchor);
				await live.reader.screenshot({ path: testInfo.outputPath(`anchor-${width}-${name.replace(/ /g, '-')}-after.png`) });
				results.push(result);
			}
		} finally {
			fs.writeFileSync(testInfo.outputPath(`anchor-drift-${testInfo.project.name}-${width}.json`), JSON.stringify(results, null, 2));
			testInfo.annotations.push({ type: 'drift', description: JSON.stringify(results) });
			await live.close();
		}
		for (const result of results) {
			expect.soft(result.maxDrift, `${result.name}: the text in view never moves`).toBeLessThanOrEqual(3);
		}
	});
}

for (const [width, height] of LAYOUTS) {
	test(`@surface:live-document remote appends follow only a reader near the end at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip((width <= 768) !== (testInfo.project.name === 'chromium-mobile'), 'One layout per project.');
		test.setTimeout(180_000);
		const document = await seedLiveDocument(request, `append-${width}`, body());
		const live = await pair(browser, document, width, height);
		try {
			await readNearEnd(live.reader, 40);
			await append(live.writer, `\n\n${BLOCK(3)}`);
			await expect(live.reader.locator('.cm-content')).toContainText('Inserted paragraph 2');
			await expect.poll(() => live.reader.locator('.cm-scroller').evaluate((element) =>
				Math.round(element.scrollHeight - element.scrollTop - element.clientHeight),
			)).toBeLessThanOrEqual(2);

			await readNearEnd(live.reader, 200);
			const before = await anchorLine(live.reader);
			await append(live.writer, `\n\nSecond append arriving below a reader farther up. ${BLOCK(3)}`);
			await expect(live.reader.locator('.cm-content')).toContainText('Second append arriving below a reader farther up.');
			const after = await anchorLine(live.reader);
			expect(after.text, 'a reader farther from the end keeps the same top line').toBe(before.text);
			expect(Math.abs(after.top - before.top), 'the held top line stays in place').toBeLessThanOrEqual(3);
		} finally {
			await live.close();
		}
	});
}
