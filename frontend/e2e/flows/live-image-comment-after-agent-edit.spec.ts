import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Browser, type Page } from '@playwright/test';
import { createSession, loginByUrl, openAppPastLanding, sendChatMessage } from '../support/corsia';
import { editorText, openLiveDocument, serverText, watchLiveFrames, type LiveDocument } from '../support/liveDocument';
import { raiseAsk } from '../support/reviewAsk';
import { holdRequest } from '../support/liveConcurrency';
import { storedComments } from '../support/liveCommentProof';
import { agentCreatesSharedDocument, demoteAgent } from '../support/sharedLiveDocument';
import { readMockCallCount, seedMockResponses } from '../support/mock';
import { bar, imageSurface, overlay } from '../support/docImageComments';
import { drag } from '../support/imageRegionComments';
import { syntheticPng } from '../support/syntheticImages';

test.use({ seedPerTest: true });

/**
 * A person comments on a region of a tall image in a live document while an agent edits lines ABOVE the image; the
 * image's own line does not change, so the comment posts on that image. The agent is a real mock-provider turn
 * calling edit_file or write_file on the shared document (the route an agent's direct edit takes into the live document);
 * nothing is injected. Each case runs at a desktop and a phone width. The edit lands either after the image is open
 * and its box drawn but before Comment (the capture), or between the press and the server's answer.
 *
 * Two hosts. 'chat' opens the document as a tab in the thread's own canvas (the control). 'activity' opens it from its
 * Activity item, the pane an agent's ask reaches the person in: there the leaf's workspace key is Activity's and the
 * document's own thread is the item's source thread, so the two differ.
 */
type Host = 'chat' | 'activity';

const TALL = { width: 1032, height: 2237 };
const promoted: string[] = [];
const COMPOSER = 'textarea[placeholder="Comment, or @ to tag an agent"]';
const layouts = [[1440, 960], [393, 852]] as const;
const ABOVE = (s: string) => `lead line ${s}`;
const added = (s: string): [string, string] => [ABOVE(s), `${ABOVE(s)}\n\nnew paragraph one ${s}\n\nnew paragraph two ${s}`];
/** Text above the image is shortened (an `edit_file` takes no empty replacement), so the image moves up. */
const deleted = (s: string): [string, string] => [`# Tall image ${s}\n\n${ABOVE(s)}`, `# ${s}`];
/** How the agent writes: `edit_file` replaces one passage; `write_file` writes the whole document again, as a rewrite of several sections does. */
type Tool = 'edit_file' | 'write_file';
const cases = [
	{ name: 'lines added above, before Comment', when: 'before', edit: added, tool: 'edit_file' },
	{ name: 'lines deleted above, before Comment', when: 'before', edit: deleted, tool: 'edit_file' },
	{ name: 'lines added above, between the press and the answer', when: 'between', edit: added, tool: 'edit_file' },
	{ name: 'whole document written again with new lines above, before Comment', when: 'before', edit: added, tool: 'write_file' },
	{ name: 'whole document written again with new lines above, between the press and the answer', when: 'between', edit: added, tool: 'write_file' },
] as const satisfies ReadonlyArray<{ name: string; when: 'before' | 'between'; edit: (s: string) => [string, string]; tool: Tool }>;
/** The Activity cases: an agent's `edit_file` adds lines above the image, before Comment and between the press and the answer. */
const activityCases = [cases[0], cases[2]] as const;

test.beforeEach(({}, info) => { test.skip(info.project.name === 'chromium-mobile', 'Both widths run here, in contexts sized by the case.'); });
test.afterEach(async ({ request }) => {
	for (const id of promoted.splice(0)) await demoteAgent(request, id);
});

/** The agent reads the document, then makes one edit; resolves once the edit is in the file and in the viewer's open editor. */
async function agentEdits(request: APIRequestContext, viewer: Page, agent: { id: string; page: Page }, live: LiveDocument, from: string, to: string, stamp: string, tool: Tool) {
	const call = tool === 'edit_file'
		? { path: live.docPath, old_string: from, new_string: to }
		: { path: live.docPath, content: live.body.replace(from, to), description: 'Tall image e2e fixture', update_trigger: 'when the tall image spec changes' };
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${stamp}`, name: 'read_file', arguments: JSON.stringify({ path: live.docPath }) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ tool_calls: [{ id: `edit-${stamp}`, name: tool, arguments: JSON.stringify(call) }], finish_reason: 'tool_calls', match_thread: agent.id },
		{ content: 'done', finish_reason: 'stop', match_thread: agent.id },
	]);
	const before = await readMockCallCount(request);
	await sendChatMessage(agent.page, 'Edit the document.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBeGreaterThanOrEqual(before + 3);
	await expect.poll(async () => (await serverText(request, live)) !== live.body, { timeout: 20_000 }).toBe(true);
	const edited = await serverText(request, live);
	await viewer.bringToFront();
	await expect.poll(() => editorText(viewer), { timeout: 20_000, message: 'the edit reached the open viewer' }).toBe(edited);
}

async function open(browser: Browser, request: APIRequestContext, stamp: string, width: number, height: number, host: Host) {
	const agentContext = await browser.newContext({ viewport: { width: 1440, height: 960 } });
	const id = await createSession(request, `Writer ${stamp}`);
	promoted.push(id);
	const page = await agentContext.newPage();
	await loginByUrl(page);
	await page.locator('.thread-item').filter({ hasText: `Writer ${stamp}` }).click({ timeout: 10_000 });
	const agent = { id, title: `Writer ${stamp}`, page };
	const markdown = `![Tall screen ${stamp}](shots/tall-${stamp}.png)`;
	const body = `# Tall image ${stamp}\n\n${ABOVE(stamp)}\n\n${markdown}\n\ntail line ${stamp}\n`;
	const live = await agentCreatesSharedDocument(request, agent, `tall-image-${stamp}`, body);
	const shots = path.join(path.dirname(live.docPath), 'shots');
	fs.mkdirSync(shots, { recursive: true });
	fs.writeFileSync(path.join(shots, `tall-${stamp}.png`), syntheticPng(TALL.width, TALL.height));
	const viewerContext = await browser.newContext({ viewport: { width, height } });
	const viewer = await viewerContext.newPage();
	if (host === 'chat') await openLiveDocument(viewer, live, ABOVE(stamp));
	else await openFromActivity(viewer, live, stamp);
	await viewer.bringToFront();
	return { contexts: [agentContext, viewerContext], viewer, agent, live, markdown };
}

/** The agent's ask on the document, opened from its Activity item; resolves once the document is on screen and its live connection is up. */
async function openFromActivity(page: Page, live: LiveDocument, stamp: string) {
	const frames = watchLiveFrames(page);
	raiseAsk(live.threadId, live.artifactId, `Review tall image ${stamp}`);
	await openAppPastLanding(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	const row = page.locator('[data-activity-row]').filter({ hasText: `Review tall image ${stamp}` });
	await expect(row).toBeVisible();
	if ((page.viewportSize()?.width ?? 0) <= 768) await row.click();
	await expect(page.locator('.pane-col .cm-content')).toContainText(ABOVE(stamp));
	await expect.poll(() => frames.opened.has(live.artifactId), { timeout: 20_000 }).toBe(true);
}

/** Opens the document's image in the viewer, draws a box on it and types the comment; the post is left to the caller. */
async function drawAndType(page: Page, text: string) {
	const img = page.locator('.cm-md-image img').first();
	await expect(img).toBeVisible();
	await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(TALL.width);
	await img.scrollIntoViewIfNeeded();
	await img.click({ position: await img.evaluate((el) => { const b = el.getBoundingClientRect(); return { x: b.width * 0.5, y: b.height * 0.5 }; }) });
	await expect(overlay(page)).toBeVisible();
	await expect.poll(() => overlay(page).locator('.frame img').evaluate((el: HTMLImageElement) => el.naturalWidth)).toBe(TALL.width);
	let last = '';
	await expect.poll(async () => { const now = JSON.stringify(await imageSurface(page).boundingBox()); const same = now === last; last = now; return same; }, { intervals: [150] }).toBe(true);
	await bar(page).locator('[data-image-comment]').click();
	const box = (await imageSurface(page).boundingBox())!;
	await drag(page, { x: box.x + box.width * 0.2, y: box.y + box.height * 0.25 }, { x: box.x + box.width * 0.45, y: box.y + box.height * 0.5 });
	await expect(page.locator(COMPOSER)).toBeVisible();
	await page.locator(COMPOSER).fill(text);
}

const runs = [
	...cases.map((item) => ({ item, host: 'chat' as Host })),
	...activityCases.map((item) => ({ item, host: 'activity' as Host })),
];
for (const [width, height] of layouts) for (const { item, host } of runs) {
	test(`@surface:live-document @comment-proof a region comment on a tall image posts after an agent edits above it: ${host === 'activity' ? 'opened from its Activity item, ' : ''}${item.name} @${width}`, async ({ browser, request }, testInfo) => {
		test.setTimeout(240_000);
		const stamp = `${Date.now()}`, text = `region comment ${stamp}`, [from, to] = item.edit(stamp);
		const { contexts, viewer, agent, live, markdown } = await open(browser, request, stamp, width, height, host);
		const answers: string[] = [];
		/** Per post: the offsets the client sent, and where the image's markdown is in the server's text when the answer came back. */
		const offsets: Array<{ sent: { from_off?: number; to_off?: number }; imageAt: { from: number; to: number } | null }> = [];
		viewer.on('response', async (response) => {
			if (response.request().method() !== 'POST' || new URL(response.url()).pathname !== `/api/artifacts/${live.artifactId}/comments`) return;
			answers.push(`${response.status()} ${(await response.text().catch(() => '')).slice(0, 300)}`);
			const sent = (() => { try { return JSON.parse(response.request().postData() ?? '{}') as { from_off?: number; to_off?: number }; } catch { return {}; } })();
			const at = (await serverText(request, live)).indexOf(markdown);
			offsets.push({ sent: { from_off: sent.from_off, to_off: sent.to_off }, imageAt: at < 0 ? null : { from: at, to: at + markdown.length } });
		});
		try {
			await drawAndType(viewer, text);
			if (item.when === 'before') {
				await agentEdits(request, viewer, agent, live, from, to, stamp, item.tool);
				await viewer.locator(COMPOSER).press('ControlOrMeta+Enter');
			} else {
				const held = await holdRequest(viewer, `**/api/artifacts/${live.artifactId}/comments*`);
				await viewer.locator(COMPOSER).press('ControlOrMeta+Enter');
				await held.arrived;
				await agentEdits(request, viewer, agent, live, from, to, stamp, item.tool);
				held.release();
			}
			await expect.poll(async () => (await viewer.locator(COMPOSER).count()) === 0 || answers.some(answer => !answer.startsWith('200')), { timeout: 20_000 }).toBe(true);
			await expect.poll(() => offsets.length, { timeout: 5_000 }).toBe(answers.length);
			await testInfo.attach('comment-post-answers', { body: JSON.stringify({ answers, offsets }, null, 2), contentType: 'application/json' });
			expect(answers.filter(answer => !answer.startsWith('200')), 'the server refused the post').toEqual([]);
			await expect(viewer.locator(COMPOSER)).toHaveCount(0);
			await expect.poll(() => storedComments(request, live)).toEqual([expect.objectContaining({ body: text, anchor_text: markdown })]);
			await viewer.screenshot({ path: testInfo.outputPath(`image-after-agent-edit-${host}-${width}.png`), animations: 'disabled' });
		} finally { for (const context of contexts) await context.close(); }
	});
}
