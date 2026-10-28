import { readFile, writeFile } from 'node:fs/promises';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { setDeliveryPreference } from '../support/heldComments';
import { openLiveDocument, type LiveDocument } from '../support/liveDocument';
import { agentCreatesSharedDocument, demoteAgent, designateConductor } from '../support/sharedLiveDocument';
import { layouts } from '../support/liveConcurrency';
import { readMockCallCount, readUnmatchedThreads, releaseMockGate, seedMockResponses } from '../support/mock';
import { beginComment } from '../support/liveCommentProof';
import { expectCapability, watchSocket } from '../support/documentOpen';

/**
 * Design 6.1 acceptance on the real editor: an agent live write is typed in at that agent's caret.
 * Each writer is a real mock-provider turn (read_file, then edit_file) that lands through the live lane,
 * so the server names the writer on the broadcast the viewer applies. Nothing is injected into the page.
 */

const PACE_CAP_MS = 12_000;
const MARKER_A = 'MARKER-ALPHA';
const MARKER_B = 'MARKER-BETA';

function prose(stamp: string, words: number): string {
	return Array.from({ length: words }, (_, index) => `w${index}${stamp.slice(-2)}`).join(' ');
}

interface Agent { id: string; title: string; page: Page }

/** Every agent a case promoted; the rail lists only a few before "Show more", so each case demotes its own. */
const promoted: string[] = [];

test.afterEach(async ({ request }) => {
	for (const id of promoted.splice(0)) await demoteAgent(request, id);
});

/** A second page on the agent's own thread, ready to send its one scripted turn. */
async function openAgent(request: APIRequestContext, viewer: Page, title: string): Promise<Agent> {
	const id = await createSession(request, title);
	promoted.push(id);
	const page = await viewer.context().newPage();
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	return { id, title, page };
}

/**
 * The agent reads the document, then replaces `marker` with `text`; resolves once the turn has taken all three replies.
 * `wakes` are the threads the edit is expected to wake (the owner, when another agent edits); any other model call fails.
 */
async function agentWrites(request: APIRequestContext, agent: Agent, live: LiveDocument, marker: string, text: string, wakes: string[] = []): Promise<void> {
	const stamp = `${agent.id}-${Date.now()}`;
	// Bound to the writer: its edit wakes the document's owner, and that turn must not take or count these replies.
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ tool_calls: [{ id: `edit-${stamp}`, name: 'edit_file', arguments: JSON.stringify({ path: live.docPath, old_string: marker, new_string: text }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ content: `Edited by ${agent.title}.`, finish_reason: 'stop', match_thread: agent.id },
	]);
	await sendChatMessage(agent.page, 'Edit the document.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
	for (const thread of await readUnmatchedThreads(request)) expect(wakes, 'an unexpected thread called the model').toContain(thread);
}

function caret(page: Page, agent: Agent) {
	return page.locator(`.cm-presence-cursor[data-thread-id="${agent.id}"]`);
}

async function shownCount(page: Page, text: string): Promise<number> {
	return ((await page.locator('.cm-content').textContent()) ?? '').split(text).length - 1;
}

/** The writing agent creates the document it then edits, so it owns it; the viewer opens it afterwards. */
async function seed(request: APIRequestContext, owner: Agent, stamp: string): Promise<LiveDocument> {
	return agentCreatesSharedDocument(request, owner, `typed-${stamp}`, `# Typed ${stamp}\n\nlead line ${stamp}\n\n${MARKER_A}\n\n${MARKER_B}\n\nclosing line ${stamp}\n`);
}

for (const [width, height, project] of layouts) {
	test(`@surface:live-document an agent file edit preserves a focused comment draft at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const viewer = await context.newPage();
			const socket = watchSocket(viewer);
			await setDeliveryPreference(request, true);
			const agent = await openAgent(request, viewer, `Draft Agent ${stamp}`);
			const live = await seed(request, agent, stamp);
			const frames = await openLiveDocument(viewer, live);
			const quote = `lead line ${stamp}`, body = `Unfinished ${stamp}`;
			await beginComment(viewer, quote, body);
			const input = viewer.getByPlaceholder('Comment…');
			await expect(input).toBeFocused();
			const editorNode = await viewer.locator('.cm-content').elementHandle();
			await seedMockResponses(request, [
				{ tool_calls: [{ id: `show-${stamp}`, name: 'document_open', arguments: JSON.stringify({ artifact_id: live.artifactId }) }], completion_gate: `show-${stamp}`, finish_reason: 'tool_calls', match_thread: agent.id },
				{ tool_calls: [{ id: `register-${stamp}`, name: 'artifact_register', arguments: JSON.stringify({ path: live.docPath, description: `Updated ${stamp}` }) }], finish_reason: 'tool_calls', match_thread: agent.id },
				{ content: 'Updated document registry.', finish_reason: 'stop', match_thread: agent.id },
			]);
			await sendChatMessage(agent.page, 'Show and register the document.');
			await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(1);
			await viewer.bringToFront();
			await input.focus();
			await expectCapability(socket, agent.id, { canShow: false, visibleArtifactId: live.artifactId });
			await releaseMockGate(request, `show-${stamp}`);
			await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
			await expect.poll(() => socket.results, { timeout: 10_000 }).toContainEqual(expect.objectContaining({ outcome: 'already_open' }));
			await expect.poll(async () => {
				const response = await request.get('/api/artifacts/resolve', { params: { token: TEST_TOKEN, path: live.docPath } });
				return response.ok() ? (await response.json()).artifact?.description : null;
			}).toBe(`Updated ${stamp}`);
			expect(await editorNode!.evaluate(node => node.isConnected), 'the viewer stays mounted across the agent turn').toBe(true);
			await expect(input).toHaveValue(body);
			await expect(input).toBeFocused();
			await agentWrites(request, agent, live, MARKER_A, `Edited elsewhere ${stamp}`);
			await expect(viewer.locator('.cm-content')).toContainText(`Edited elsewhere ${stamp}`, { timeout: 20_000 });
			await expect(input).toBeVisible();
			await expect(input).toHaveValue(body);
			await expect(input).toBeFocused();
			await viewer.keyboard.type(' continued');
			await expect(input).toHaveValue(`${body} continued`);
			await expect(viewer.locator('.cm-content')).toContainText(quote);
			// A process outside CORSIA writes the disposable file. The next lane write imports it first.
			const disk = await readFile(live.docPath, 'utf8');
			await writeFile(live.docPath, disk.replace(MARKER_B, `Outside disk ${stamp}`));
			await agentWrites(request, agent, live, `closing line ${stamp}`, `Closing edited ${stamp}`);
			await expect(viewer.locator('.cm-content')).toContainText(`Outside disk ${stamp}`);
			await expect(input).toHaveValue(`${body} continued`);
			await expect(input).toBeFocused();
			expect(frames.opened.get(live.artifactId)).toBe(1);
			const params = { token: TEST_TOKEN };
			const agentComment = async (data: Record<string, unknown>) => {
				const response = await request.post('/api/delegation/docs/comments', { params, data: { as_session_id: agent.id, ...data } });
				expect(response.ok(), await response.text()).toBeTruthy();
				return (await response.json()).comment.id as string;
			};
			const intact = async () => { await expect(input).toBeVisible(); await expect(input).toHaveValue(`${body} continued`); await expect(input).toBeFocused(); };
			const root = await agentComment({ artifact_id: live.artifactId, quote: `Closing edited ${stamp}`, body: 'Agent comment' });
			await expect(viewer.locator('.cm-comment-anchor')).not.toHaveCount(0);
			await intact();
			await agentComment({ parent_id: root, body: 'Agent reply' });
			await intact();
			const resolved = await request.patch(`/api/delegation/docs/comments/${root}`, { params, data: { as_session_id: agent.id, resolved: true } });
			expect(resolved.ok(), await resolved.text()).toBeTruthy();
			await intact();
			const suggestion = await agentComment({ artifact_id: live.artifactId, quote: `Outside disk ${stamp}`, body: 'Agent suggestion', replacement: `Accepted ${stamp}` });
			await intact();
			const accepted = await request.post(`/api/comments/${suggestion}/decision`, { params, data: { decision: 'accept' } });
			expect(accepted.ok(), await accepted.text()).toBeTruthy();
			await expect(viewer.locator('.cm-content')).toContainText(`Accepted ${stamp}`);
			await intact();
			await viewer.screenshot({ path: testInfo.outputPath(`agent-edit-draft-${width}.png`), animations: 'disabled' });
		} finally { await context.close(); }
	});

	test(`@surface:live-document an agent write types in at the agent caret, then the caret blinks idle at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const viewer = await context.newPage();
			const agent = await openAgent(request, viewer, `Typing Agent ${stamp}`);
			const live = await seed(request, agent, stamp);
			await openLiveDocument(viewer, live);
			const text = prose(stamp, 120);
			const last = text.split(' ').at(-1)!;
			await agentWrites(request, agent, live, MARKER_A, text);

			await expect(caret(viewer, agent)).toHaveCount(1);
			await expect(caret(viewer, agent).locator('.cm-presence-cursor-label')).toHaveText(agent.title);
			await expect(caret(viewer, agent)).toHaveClass(/cm-presence-cursor-typing/);
			expect(await shownCount(viewer, last), 'the end of the write is not shown yet').toBe(0);

			await expect.poll(() => shownCount(viewer, last), { timeout: PACE_CAP_MS + 3_000 }).toBe(1);
			// Idle: the steady caret goes back to blinking.
			await expect(caret(viewer, agent)).not.toHaveClass(/cm-presence-cursor-typing/);
			await expect.poll(() => caret(viewer, agent).evaluate((el) => getComputedStyle(el).animationName)).toContain('cm-presence-blink');
			await viewer.screenshot({ path: testInfo.outputPath(`live-typed-reveal-idle-${width}.png`) });
		} finally {
			await context.close();
		}
	});

	test(`@surface:live-document a click inside the typing span shows the rest at once at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const viewer = await context.newPage();
			const agent = await openAgent(request, viewer, `Click Agent ${stamp}`);
			const live = await seed(request, agent, stamp);
			await openLiveDocument(viewer, live);
			const text = prose(stamp, 400);
			const last = text.split(' ').at(-1)!;
			await agentWrites(request, agent, live, MARKER_A, text);
			await expect(caret(viewer, agent)).toHaveClass(/cm-presence-cursor-typing/);
			expect(await shownCount(viewer, last)).toBe(0);

			const line = viewer.locator('.cm-line').filter({ has: caret(viewer, agent) });
			const box = await line.boundingBox();
			expect(box).not.toBeNull();
			await line.click({ position: { x: Math.max(1, box!.width - 2), y: 4 } });
			await expect.poll(() => shownCount(viewer, last), { timeout: 3_000, message: 'the whole write shows far inside the pace cap' }).toBe(1);
			await expect(viewer.locator('.cm-presence-cursor-typing')).toHaveCount(0);
		} finally {
			await context.close();
		}
	});

	test(`@surface:live-document reduced motion shows an agent write at once at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const viewer = await context.newPage();
			await viewer.emulateMedia({ reducedMotion: 'reduce' });
			const agent = await openAgent(request, viewer, `Calm Agent ${stamp}`);
			const live = await seed(request, agent, stamp);
			await openLiveDocument(viewer, live);
			const text = prose(stamp, 400);
			const last = text.split(' ').at(-1)!;
			await agentWrites(request, agent, live, MARKER_A, text);
			await expect.poll(() => shownCount(viewer, last), { timeout: 3_000 }).toBe(1);
			await expect(viewer.locator('.cm-presence-cursor-typing')).toHaveCount(0);
		} finally {
			await context.close();
		}
	});

	test(`@surface:live-document two agents writing at once both type in their own lanes at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(150_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		let designated = false;
		try {
			const viewer = await context.newPage();
			const first = await openAgent(request, viewer, `Lane One ${stamp}`);
			const second = await openAgent(request, viewer, `Lane Two ${stamp}`);
			const live = await seed(request, first, stamp);
			// Only the owner or the conductor edits a shared document directly; the second writer is the conductor.
			await designateConductor(request, second.id);
			designated = true;
			await openLiveDocument(viewer, live);
			const textA = prose(`a${stamp}`, 500);
			const textB = prose(`b${stamp}`, 500);
			const lastA = textA.split(' ').at(-1)!;
			const lastB = textB.split(' ').at(-1)!;
			await agentWrites(request, first, live, MARKER_A, textA);
			await agentWrites(request, second, live, MARKER_B, textB, [first.id]);

			await expect(caret(viewer, first)).toHaveClass(/cm-presence-cursor-typing/);
			await expect(caret(viewer, second)).toHaveClass(/cm-presence-cursor-typing/);
			await expect(caret(viewer, first).locator('.cm-presence-cursor-label')).toHaveText(first.title);
			await expect(caret(viewer, second).locator('.cm-presence-cursor-label')).toHaveText(second.title);
			expect(await shownCount(viewer, lastA) + await shownCount(viewer, lastB), 'neither write is complete yet').toBe(0);

			await expect.poll(async () => [await shownCount(viewer, lastA), await shownCount(viewer, lastB)], { timeout: PACE_CAP_MS + 5_000 }).toEqual([1, 1]);
		} finally {
			// A designated CORSIA agent changes where the app lands; later cases expect none.
			if (designated) await designateConductor(request, null);
			await context.close();
		}
	});

	test(`@surface:live-document nothing scrolls sideways while an agent label sits near the right edge at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const viewer = await context.newPage();
			const agent = await openAgent(request, viewer, `A Long Agent Name That Crowds The Edge ${stamp}`);
			const live = await seed(request, agent, stamp);
			await openLiveDocument(viewer, live);
			const text = prose(stamp, 400);
			const last = text.split(' ').at(-1)!;
			await agentWrites(request, agent, live, MARKER_A, text);
			await expect(caret(viewer, agent)).toHaveCount(1);

			// Read what the next frame paints: resize observations run after every animation-frame
			// callback (the editor's label clamp included) and layout, just before paint. A read taken
			// between an editor update and its measure frame sees a label that is never painted there.
			const overflow = async () => viewer.evaluate(() => new Promise<{ page: number; scroller: number; labelPastEdge: number; widest: string }>((resolve) => {
				const probe = document.body.appendChild(document.createElement('div'));
				const observer = new ResizeObserver(() => {
					observer.disconnect();
					probe.remove();
					const scrollDOM = document.querySelector('.cm-scroller')!;
					const scroller = scrollDOM.getBoundingClientRect();
					const label = document.querySelector('.cm-presence-cursor-label')?.getBoundingClientRect();
					const root = document.documentElement;
					// Name whatever reaches past the scroller's client area, so a failure says what overflowed.
					const clientRight = scroller.left + scrollDOM.clientLeft + scrollDOM.clientWidth;
					let widest = '';
					let far = clientRight;
					for (const el of Array.from(scrollDOM.querySelectorAll<HTMLElement>('*'))) {
						const r = el.getBoundingClientRect().right;
						if (r > far) { far = r; widest = `${el.tagName}.${el.className} +${Math.round(r - clientRight)}`; }
					}
					resolve({
						widest,
						page: root.scrollWidth - root.clientWidth,
						scroller: scrollDOM.scrollWidth - scrollDOM.clientWidth,
						labelPastEdge: label ? Math.max(0, label.right - scroller.right) : 0,
					});
				});
				observer.observe(probe);
			}));
			// The caret sweeps every line end while it types; sample until the write is complete.
			for (let sample = 0; sample < 40 && await shownCount(viewer, last) === 0; sample += 1) {
				const seen = await overflow();
				expect(seen.page, 'the page never scrolls sideways').toBeLessThanOrEqual(0);
				expect(seen.scroller, `the editor never scrolls sideways (${seen.widest})`).toBeLessThanOrEqual(0);
				expect(seen.labelPastEdge, 'the label stays inside the editor').toBeLessThanOrEqual(1);
				await viewer.waitForTimeout(300);
			}
			await viewer.screenshot({ path: testInfo.outputPath(`live-typed-reveal-label-${width}.png`) });
		} finally {
			await context.close();
		}
	});
}
