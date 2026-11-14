import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, replacePreviousFixtureThread, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';

// The bottom notices (the jump control, and a below activity pill) float over a document's foot. The text never moves or shrinks as
// one comes or goes: a line on screen stays exactly where it is. While one shows, a line the caret moves onto stops above it, and
// the last line still scrolls clear of it.
const TAG = '@surface:doc-bottom-notices';
const BODY = [
	'# Saturday work day', '',
	'Thanks for the count. Twelve of us can come Saturday, so the two extra pairs of loppers from the shed will be plenty.', '',
	...Array.from({ length: 10 }, (_, i) => [`Item ${i + 1}: I will bring the sign-in sheet, a cooler of water and the spare gloves, and check the gate code at the north trailhead lot before everyone arrives.`, '']).flat(),
	'Thanks,', 'Alex', '',
].join('\n');
const QUOTE = 'Alex';
const FIRST_QUOTE = 'Saturday work day';

const WIDTHS = [
	{ width: 1440, height: 900, mobile: false },
	{ width: 393, height: 852, mobile: true },
];

function skipOtherProject(mobile: boolean, projectName: string) {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || mobile !== projectName.endsWith('mobile'), 'Each device project checks its own width.');
}

/** The reader's comment on the document's last line, below the fold at every checked width, or on its first line. Returns its id. */
async function commentOnLastLine(request: APIRequestContext, artifactId: string, anchor: 'last' | 'first' = 'last'): Promise<string> {
	const quote = anchor === 'last' ? QUOTE : FIRST_QUOTE;
	const from = anchor === 'last' ? BODY.lastIndexOf(QUOTE) : BODY.indexOf(FIRST_QUOTE);
	const made = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'Can we confirm the gate code?', anchor_text: quote, from_off: from, to_off: from + quote.length, delivery_policy: 'immediate' },
	});
	expect(made.ok(), await made.text()).toBeTruthy();
	return (await made.json()).comment.id as string;
}

/** An agent's reply the reader has not seen, so the jump control points down to that thread. Returns the reply's id. */
function addAgentReply(artifactId: string, parent: string, thread: string): string {
	const id = randomUUID().replace(/-/g, '');
	isolatedDb([
		"connection.execute(\"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (?, ?, ?, 'Reviewer', 'agent', ?, 'The code is 4417.', strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 second'), strftime('%Y-%m-%dT%H:%M:%f+00:00','now','+1 second'))\", (sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]))",
	], id, artifactId, parent, thread);
	return id;
}

async function reloadComments(page: Page, artifactId: string) {
	await page.evaluate(async (id) => (await import('/src/lib/stores/docComments.ts')).loadComments(id), artifactId);
}

async function openDocument(page: Page, request: APIRequestContext, label: string, unread: boolean, anchor: 'last' | 'first' = 'last') {
	const title = `Bottom notices ${label} ${Date.now().toString(36)}`;
	const thread = await createSession(request, title);
	await replacePreviousFixtureThread(request, thread);
	const params = { token: TEST_TOKEN };
	const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
	const workspace = roots.find(root => root.kind === 'workspace')?.path;
	if (!workspace) throw new Error('Disposable workspace was not returned by the mock backend');
	const name = `notices-${thread.replace(/[^0-9a-f]/g, '').padEnd(12, '0').slice(0, 12)}.md`;
	const docPath = path.join(workspace, 'notices', name);
	const saved = await request.put('/api/files/content', { params, data: { path: docPath, text: BODY, thread_id: thread, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const comment = await commentOnLastLine(request, artifactId, anchor);
	if (unread) addAgentReply(artifactId, comment, thread);
	await page.addInitScript(({ thread, artifactId, docPath, name }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'notices', artifactId, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'notices', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
	}, { thread, artifactId, docPath, name });
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	// The editor draws only the lines near the view, so the document is known open by its first line, not its last.
	await expect(page.locator('.cm-content')).toContainText('Saturday work day');
	return { artifactId, comment, thread };
}

const jump = (page: Page) => page.locator('[data-doc-bottom-band] [data-doc-jump]');
const scroller = (page: Page) => page.locator('.doc .cm-scroller');
const frames = (page: Page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));

/** The box of the row holding the caret, in the window: a wrapped paragraph is one line element over several rows. */
function caretRow(page: Page) {
	return page.evaluate(() => {
		const selection = getSelection();
		if (!selection?.rangeCount) return null;
		const rect = selection.getRangeAt(0).getBoundingClientRect();
		if (rect.height > 0) return { top: rect.top, bottom: rect.bottom };
		// An empty line has no text to measure a caret in; it is a single row.
		const node = selection.focusNode;
		const line = (node instanceof Element ? node : node?.parentElement)?.closest('.cm-line');
		if (!line) return null;
		const box = line.getBoundingClientRect();
		return { top: box.top, bottom: box.bottom };
	});
}

/** Walks the caret down from the first line until its row ends within `reach` of the editor's foot. */
async function caretToFoot(page: Page, reach: number) {
	const foot = await scroller(page).evaluate((node) => node.getBoundingClientRect().bottom);
	await page.locator('.cm-line').first().click();
	for (let step = 0; step < 120; step++) {
		const row = await caretRow(page);
		if (row && row.bottom > foot - reach) break;
		await page.keyboard.press('ArrowDown');
	}
	await frames(page);
	return foot;
}

for (const { width, height, mobile } of WIDTHS) {
	test(`${TAG} ${width}px: a line on screen stays to the pixel as the jump control comes and goes`, async ({ page, request }, info) => {
		skipOtherProject(mobile, info.project.name);
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height });
		const { artifactId, comment, thread } = await openDocument(page, request, `toggle-${width}`, false);
		await expect(page.locator('.doc'), 'no notice shows yet').not.toHaveAttribute('data-notice-jump');
		const foot = await caretToFoot(page, 40);
		const before = { row: await caretRow(page), top: await scroller(page).evaluate((node) => node.scrollTop) };
		expect(before.row, 'the caret row is on screen, at the foot').not.toBeNull();
		expect(before.row!.bottom).toBeLessThanOrEqual(foot);

		const reply = addAgentReply(artifactId, comment, thread);
		await reloadComments(page, artifactId);
		await expect(jump(page), 'the jump control points at the new reply below').toBeVisible();
		await expect(page.locator('.doc')).toHaveAttribute('data-notice-jump', '');
		await frames(page);
		expect(await caretRow(page), 'the caret row does not move as the jump control comes').toEqual(before.row);
		expect(await scroller(page).evaluate((node) => node.scrollTop)).toBe(before.top);

		isolatedDb(['connection.execute("DELETE FROM doc_comments WHERE id = ?", (sys.argv[2],))'], reply);
		await reloadComments(page, artifactId);
		await expect(jump(page), 'the jump control leaves with the reply').toHaveCount(0);
		await expect(page.locator('.doc')).not.toHaveAttribute('data-notice-jump');
		await frames(page);
		expect(await caretRow(page), 'the caret row does not move as the jump control goes').toEqual(before.row);
		expect(await scroller(page).evaluate((node) => node.scrollTop)).toBe(before.top);
	});

	test(`${TAG} ${width}px: scrolled to the end, the last line ends above the jump control`, async ({ page, request }, info) => {
		skipOtherProject(mobile, info.project.name);
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height });
		// The unread reply is on the first line, so it is off screen once the view is at the end and the jump control stays (a reply on the
		// last line is on screen there, and the control by design fades out as the view arrives).
		await openDocument(page, request, `end-${width}`, true, 'first');
		await scroller(page).evaluate((node) => { node.scrollTop = node.scrollHeight; });
		await frames(page);
		await expect(jump(page), 'the jump control points up at the unread reply on the first line').toBeVisible();
		const lastLine = await page.locator('.cm-line').last().evaluate((line) => line.getBoundingClientRect().bottom);
		const lastText = await page.locator('.cm-line', { hasText: QUOTE }).last().evaluate((line) => line.getBoundingClientRect().bottom);
		const jumpTop = await jump(page).evaluate((el) => el.getBoundingClientRect().top);
		expect(Math.max(lastLine, lastText), 'the last line ends above the jump control').toBeLessThanOrEqual(jumpTop);
	});
}

test(`${TAG} 393px: a line the caret moves onto stops above the jump control while it shows`, async ({ page, request }, info) => {
	skipOtherProject(true, info.project.name);
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 393, height: 852 });
	await openDocument(page, request, 'caret', true);
	await expect(jump(page), 'the jump control points at the unread reply below').toBeVisible();
	await caretToFoot(page, 100);
	const before = await scroller(page).evaluate((node) => node.scrollTop);
	for (let step = 0; step < 3; step++) await page.keyboard.press('ArrowDown');
	await frames(page);
	const row = await caretRow(page);
	await expect(jump(page), 'the jump control still shows').toBeVisible();
	const jumpTop = await jump(page).evaluate((el) => el.getBoundingClientRect().top);

	expect(await scroller(page).evaluate((node) => node.scrollTop), 'the view scrolled to follow the caret').toBeGreaterThan(before);
	expect(row, 'the caret row is measured').not.toBeNull();
	expect(row?.bottom ?? Infinity, 'the caret row ends above the jump control').toBeLessThanOrEqual(jumpTop);
});
