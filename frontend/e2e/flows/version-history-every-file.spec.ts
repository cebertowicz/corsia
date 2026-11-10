import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import type { APIRequestContext, Browser, Page, TestInfo } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';

const authQuery = `?${['to', 'ken'].join('')}=${encodeURIComponent(TEST_TOKEN)}`;
const FIRST = 'Plan for the week.\nKeep this line.\nDelete this line.\n';
const EDITS = ['Plan for the week.\nKeep this line.\n', 'Plan for the week.\nKeep this line.\nAdded line.\n'];
const THIRD = `${EDITS[1]}Another line.\n`;

test.setTimeout(120_000);

async function workspaceRoot(request: APIRequestContext): Promise<string> {
	const tree = await (await request.get(`/api/files/tree${authQuery}`)).json();
	const root = tree.roots.find((entry: { kind: string }) => entry.kind === 'workspace').path as string;
	execFileSync('git', ['init', '-q', root]);
	// As in a person's workspace: chat folders are ignored, so only registration marks a file there as kept.
	const ignore = path.join(root, '.gitignore');
	const rules = fs.existsSync(ignore) ? fs.readFileSync(ignore, 'utf8') : '';
	if (!rules.includes('/.threads/*/*')) fs.writeFileSync(ignore, `${rules}/.threads/*/*\n`);
	return root;
}

function commits(root: string, file: string): number {
	try {
		return execFileSync('git', ['-C', root, 'log', '--format=%H', '--', path.relative(root, file)], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).split('\n').filter(Boolean).length;
	} catch {
		return 0; // No commit in the workspace yet.
	}
}

// A save with a chat registers the file to it; without one it is a plain save.
async function save(request: APIRequestContext, file: string, text: string, threadId: string | null) {
	const response = await request.put(`/api/files/content${authQuery}`, { data: { path: file, text, thread_id: threadId } });
	expect(response.ok(), await response.text()).toBe(true);
}

async function register(request: APIRequestContext, file: string, threadId: string): Promise<string> {
	const registered = await request.post(`/api/files/register${authQuery}`, { data: { path: file, thread_id: threadId } });
	expect(registered.ok(), await registered.text()).toBe(true);
	return (await registered.json()).artifact.id as string;
}

// Opens one document tab in the chat's canvas, with the file tray tucked away on a wide screen.
// The canvas opens it with the chat, which registers the file to that chat.
async function openDocument(page: Page, info: TestInfo, title: string, artifactId: string | null, file: string, threadId: string) {
	const phone = info.project.name === 'chromium-mobile';
	// The newest init script runs last, so each call opens its own file.
	await page.context().addInitScript(({ id, tabPath, thread, name }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [thread]: { tabs: [{ instanceId: 'tab-doc', artifactId: id, path: tabPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { id: artifactId, tabPath: file, thread: threadId, name: path.basename(file) });
	await loginByUrl(page, { waitForComposer: false });
	await openChat(page, title);
	await expect(page.locator('.cm-content')).toBeVisible();
	if (!phone) await page.getByRole('button', { name: 'Toggle panel' }).click();
}

async function openHistory(page: Page, info: TestInfo, title: string, artifactId: string, file: string, threadId: string) {
	await openDocument(page, info, title, artifactId, file, threadId);
	await page.getByRole('button', { name: 'History', exact: true }).click();
	return page.locator('[data-doc-history-drawer]');
}

// The chat's agent edits the file with a real tool call in a mock-provider turn, in its own browser.
async function agentEdit(browser: Browser, request: APIRequestContext, info: TestInfo, title: string, threadId: string, file: string, from: string, to: string) {
	const phone = info.project.name === 'chromium-mobile';
	const agent = await (await browser.newContext({ viewport: { width: phone ? 393 : 1440, height: phone ? 852 : 900 } })).newPage();
	await loginByUrl(agent);
	await openChat(agent, title);
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${title}`, name: 'read_file', arguments: JSON.stringify({ path: file }) }], finish_reason: 'tool_calls', match_thread: threadId },
		{ tool_calls: [{ id: `edit-${title}`, name: 'edit_file', arguments: JSON.stringify({ path: file, old_string: from, new_string: to }) }], finish_reason: 'tool_calls', match_thread: threadId },
		{ content: 'Edited the file.', finish_reason: 'stop', match_thread: threadId },
	]);
	await sendChatMessage(agent, 'Edit the file.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
	await expect.poll(() => fs.readFileSync(file, 'utf8'), { timeout: 15_000 }).toContain(to);
	await agent.context().close();
}

/** Opens a chat from the rail. On a phone the rail sits behind the main surface, which slides right to
 *  open it, and a run once saw it shut again while the app finished loading; so the open is retried until
 *  the chat's row takes the click. */
async function openChat(page: Page, title: string) {
	const row = page.locator('aside.sidebar .thread-item').filter({ hasText: title });
	await expect(async () => {
		const shut = ((await page.locator('main.main').boundingBox())?.x ?? 0) <= 40;
		if ((page.viewportSize()?.width ?? 1440) <= 768 && shut) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await row.click({ timeout: 3_000 });
	}).toPass({ timeout: 20_000 });
}

/** Open the right pane when it is visually closed (as in artifact-pane-pin.spec.ts): measure, since a
 *  closed pane keeps its rows in the DOM. */
async function ensurePaneOpen(page: Page) {
	const width = async () => (await page.locator('aside.pane').boundingBox())?.width ?? 0;
	if ((await width()) < 40) await page.getByRole('button', { name: 'Toggle panel' }).click();
	await expect.poll(width, { message: 'right pane did not reach open width' }).toBeGreaterThan(200);
}

function shots(width: number) {
	const out = process.env.VH_OUT;
	if (out) fs.mkdirSync(out, { recursive: true });
	return async (page: Page, name: string) => {
		if (out) await page.screenshot({ path: path.join(out, `${name}-${width}.png`), animations: 'disabled' });
	};
}

// Restores ``version`` through the same route the History surface calls.
async function restoreThroughApi(request: APIRequestContext, file: string, version: string, threadId: string) {
	const base = createHash('sha256').update(fs.readFileSync(file)).digest('hex');
	const response = await request.post(`/api/files/history/restore${authQuery}`, { data: { path: file, version, base_sha256: base, thread_id: threadId } });
	expect(response.ok(), await response.text()).toBe(true);
}

function oldestCommit(root: string, file: string): string {
	return execFileSync('git', ['-C', root, 'log', '--reverse', '--format=%H', '--', path.relative(root, file)], { encoding: 'utf8' }).split('\n')[0];
}

test('@flow:version-history-every-file a chat\'s document the person edits shows every History state, groups saves and restores', async ({ page, request, browser }, info) => {
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	const shot = shots(width);
	const title = `history-doc-${width}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = await workspaceRoot(request);
	// In the chat's own folder, so the chat owns it and its agent edits it directly: an agent may not write
	// the scope's top-level folders, and edits a shared document it does not own only by suggestion.
	const file = path.join(root, '.threads', threadId, 'reports', `plan-${width}-${Date.now()}.md`);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, FIRST);
	// Registered, as opening it in the chat's canvas does, so the ignored chat folder does not keep it out.
	const artifactId = await register(request, file, threadId);
	// An ordinary document is recorded from its first save: the text it had, then each save. Saves that
	// queue together are recorded as one, so each is let land before the next.
	for (const [index, text] of [...EDITS, THIRD].entries()) {
		await save(request, file, text, threadId);
		await expect.poll(() => commits(root, file), { timeout: 15_000 }).toBe(index + 2);
	}
	// The person restores the first text (a save with a reason), then the chat's agent edits it.
	await restoreThroughApi(request, file, oldestCommit(root, file), threadId);
	await agentEdit(browser, request, info, title, threadId, file, 'Delete this line.', 'Agent line.');
	await expect.poll(() => commits(root, file)).toBe(6);
	// History is read only once it is opened, so the first read is the one that fails.
	await page.route('**/api/files/history?*', async (route) => route.abort('internetdisconnected'), { times: 1 });
	const drawer = await openHistory(page, info, title, artifactId, file, threadId);
	await expect(page.getByText('Couldn’t load history. Check the connection and try again.')).toBeVisible();
	await shot(page, 'history-doc-load-failure');
	await page.getByRole('button', { name: 'Retry', exact: true }).click();
	const rows = drawer.locator('.vrow');
	// Newest first: the agent's edit under the chat's name; the restore, whose reason keeps it out of the
	// person's group; the person's three saves as one version; the text the file had.
	await expect(rows).toHaveCount(4);
	await expect(rows.first()).toContainText(title);
	await expect(rows.first()).toContainText('Current');
	await expect(rows.first()).not.toContainText('Unknown editor');
	await expect(rows.nth(1)).toContainText('Restored');
	await expect(rows.nth(2)).toContainText('e2e');
	await expect(rows.nth(2)).toContainText('No reason given');
	await expect(rows.nth(3)).toContainText('Unknown editor');
	await shot(page, 'history-doc-list');
	// A phone shows one version at a time, so it steps with Older; a wide screen picks the row.
	const older = page.getByRole('button', { name: 'Older', exact: true });
	const goTo = async (from: number, to: number) => {
		if (phone) for (let at = from; at < to; at++) await older.click();
		else await rows.nth(to).click();
	};
	await rows.first().click();
	await expect(page.locator('.diff-line.del')).toHaveCount(1);
	await expect(page.locator('.diff-line.del')).toContainText('Delete this line.');
	await expect(page.locator('.diff-line.add')).toContainText('Agent line.');
	await shot(page, 'history-doc-version');
	// The group's one version shows every change its three saves made, the deleted text in red.
	await goTo(0, 2);
	await expect(page.locator('.diff-line.del')).toHaveCount(1);
	await expect(page.locator('.diff-line.del')).toContainText('Delete this line.');
	await expect(page.locator('.diff-line.add', { hasText: 'Added line.' })).toBeVisible();
	await expect(page.locator('.diff-line.add', { hasText: 'Another line.' })).toBeVisible();
	await shot(page, 'history-doc-deleted-text');
	await goTo(2, 3);
	await expect(page.locator('.diff-line.del')).toHaveCount(0);
	await expect(page.locator('.diff-line.add', { hasText: 'Delete this line.' })).toBeVisible();
	await shot(page, 'history-doc-older-version');
	// Hold the restore's answer after the server has applied it: Restore reads "Restoring…", the save
	// line "Saving…", and the list waits for the answer.
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/files/history/restore*', async (route) => {
		const response = await route.fetch();
		await held;
		await route.fulfill({ response });
	}, { times: 1 });
	await page.getByRole('button', { name: 'Restore', exact: true }).click();
	await expect(page.getByRole('button', { name: 'Restoring…', exact: true })).toBeDisabled();
	await expect.poll(() => commits(root, file)).toBe(7);
	await expect.soft(page.getByText('Saving…', { exact: true })).toBeVisible();
	// A phone keeps the version open while it restores; a wide screen's list keeps its rows.
	if (!phone) await expect(rows).toHaveCount(4);
	await shot(page, 'history-doc-restoring');
	release();
	await expect(page.getByRole('button', { name: 'Restoring…', exact: true })).toHaveCount(0, { timeout: 30_000 });
	await expect(rows).toHaveCount(5);
	await expect(rows.first()).toContainText('Current');
	await expect(rows.first()).toContainText('Restored');
	await expect(page.locator('.cm-content')).toContainText('Delete this line.');
	expect(fs.readFileSync(file, 'utf8')).toBe(FIRST);
	await shot(page, 'history-doc-restored');
	// An edit made outside CORSIA after a version was opened: the restore is refused and the document is unchanged.
	const shown = await rows.count();
	if (phone && await older.isVisible()) await older.click(); else await rows.nth(2).click();
	await expect(page.getByRole('button', { name: 'Restore', exact: true })).toBeEnabled();
	fs.writeFileSync(file, `${FIRST}Outside edit after opening history.\n`);
	await page.getByRole('button', { name: 'Restore', exact: true }).click();
	await expect.soft(page.getByText('Couldn’t restore this version. The document is unchanged.')).toBeVisible({ timeout: 15_000 });
	await expect.soft(page.locator('.cm-content')).toContainText('Outside edit after opening history.');
	await expect.soft(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
	await shot(page, 'history-doc-changed-since');
	// Reported, not asserted: whether the reloaded list holds the outside edit as a version. A phone keeps
	// the version open, so it goes back to the list first.
	if (phone) await drawer.getByRole('button', { name: 'Back', exact: true }).click();
	const changed = `${width}: ${shown} before, ${await rows.count()} after; first: ${(await rows.first().innerText()).replace(/\s+/g, ' ')}`;
	info.annotations.push({ type: 'changed-since rows', description: changed });
	if (process.env.VH_OUT) fs.appendFileSync(path.join(process.env.VH_OUT, 'changed-since-rows.txt'), `${changed}\n`);
	expect(fs.readFileSync(file, 'utf8')).toContain('Outside edit after opening history.');
});

test('@flow:version-history-every-file a registered chat file has History from the tray; working/ and unregistered files show none', async ({ page, request }, info) => {
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	const shot = shots(width);
	const title = `history-tray-${width}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = await workspaceRoot(request);
	const dir = path.join(root, '.threads', threadId);
	fs.mkdirSync(path.join(dir, 'working'), { recursive: true });
	const kept = path.join(dir, `notes-${width}.md`);
	const loose = path.join(dir, `scratch-${width}.md`);
	const working = path.join(dir, 'working', `draft-${width}.md`);
	for (const each of [kept, loose, working]) fs.writeFileSync(each, FIRST);
	await register(request, kept, threadId);
	for (const [index, text] of EDITS.entries()) {
		await save(request, kept, text, threadId);
		await expect.poll(() => commits(root, kept), { timeout: 15_000 }).toBe(index + 2);
	}
	await save(request, loose, EDITS[0], null);
	// A chat's working/ file stays out of history even when a save with the chat registers it.
	const workingSave = await request.put(`/api/files/content${authQuery}`, { data: { path: working, text: EDITS[0], thread_id: threadId } });
	expect.soft(workingSave.ok(), await workingSave.text()).toBe(true);
	// Git ignores chat folders; the registered file is recorded anyway, the others never.
	expect(execFileSync('git', ['-C', root, 'check-ignore', '--no-index', '--', path.relative(root, kept)], { encoding: 'utf8' }).trim()).toBe(path.relative(root, kept));
	await page.waitForTimeout(1000);
	expect(commits(root, loose)).toBe(0);
	expect(commits(root, working)).toBe(0);
	// The route a person takes: the chat's file tray, the file, then History in its header.
	await loginByUrl(page, { waitForComposer: false });
	await openChat(page, title);
	await ensurePaneOpen(page);
	const trayRow = page.locator('aside.pane .row-outer').filter({ hasText: path.basename(kept) });
	await expect(trayRow).toBeVisible();
	await shot(page, 'history-tray');
	await trayRow.click();
	await expect(page.locator('.cm-content')).toContainText('Added line.');
	const history = page.getByRole('button', { name: 'History', exact: true });
	await expect(history).toBeVisible();
	await shot(page, 'history-tray-file-open');
	await history.click();
	const rows = page.locator('[data-doc-history-drawer] .vrow');
	await expect(rows).toHaveCount(2);
	await expect(rows.first()).toContainText('Current');
	await shot(page, 'history-thread-file-list');
	await rows.first().click();
	await expect(page.locator('.diff-line.del')).toContainText('Delete this line.');
	await shot(page, 'history-thread-file-version');
	// No version recorded, no control: the header offers no History and no empty panel. Opening the
	// unregistered file in the chat registers it, but it has nothing to list until it is saved.
	for (const [file, name] of [[working, 'history-none-working'], [loose, 'history-none-unregistered']] as const) {
		await openDocument(page, info, title, null, file, threadId);
		await expect(page.locator('.cm-content')).toContainText('Keep this line.');
		// History shows once the open settles, so give it the time it takes before reading it absent.
		await page.waitForTimeout(1500);
		await expect(page.getByRole('button', { name: 'History', exact: true })).toHaveCount(0);
		await expect(page.locator('[data-doc-history-drawer]')).toHaveCount(0);
		await shot(page, name);
	}
	expect(commits(root, loose)).toBe(0);
});

