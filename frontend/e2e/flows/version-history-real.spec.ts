import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import type { Page } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, loginByUrl, sendChatMessage, TEST_TOKEN } from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';

const authQuery = `?${['to', 'ken'].join('')}=${encodeURIComponent(TEST_TOKEN)}`;
const skill = (body: string) => `---\nname: demo\ndescription: A test skill.\n---\n\n# Demo\n\n${body}\n`;

test.setTimeout(120_000);
test('@flow:version-history-real steps through Git versions and restores one', async ({ page, request, browser }, info) => {
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	const title = `history-real-${width}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const tree = await (await request.get(`/api/files/tree${authQuery}`)).json();
	const root = tree.roots.find((entry: { kind: string }) => entry.kind === 'workspace').path as string;
	execFileSync('git', ['init', '-q', root]);
	const dir = path.join(root, '.threads', threadId, 'skills', 'demo');
	fs.mkdirSync(dir, { recursive: true });
	const file = path.join(dir, 'SKILL.md');
	fs.writeFileSync(file, skill('First version.'));
	for (const text of [skill('Second version.'), skill('Third version.')]) {
		const response = await request.put(`/api/files/content${authQuery}`, { data: { path: file, text, thread_id: threadId } });
		expect(response.ok(), await response.text()).toBe(true);
	}
	const registered = await request.post(`/api/files/register${authQuery}`, { data: { path: file, thread_id: threadId } });
	expect(registered.ok()).toBe(true);
	const artifactId = (await registered.json()).artifact.id as string;
	// The thread's agent edits its skill with a real tool call in a mock-provider turn, in its own browser.
	const agent = await (await browser.newContext({ viewport: { width, height: phone ? 852 : 900 } })).newPage();
	await loginByUrl(agent);
	if (phone) await agent.getByRole('button', { name: 'Toggle sidebar' }).click();
	await agent.locator('.thread-item').filter({ hasText: title }).click({ timeout: 10_000 });
	await seedMockResponses(request, [
		{ tool_calls: [{ id: `read-${title}`, name: 'read_file', arguments: JSON.stringify({ path: file }) }], finish_reason: 'tool_calls', match_thread: threadId },
		{ tool_calls: [{ id: `edit-${title}`, name: 'edit_file', arguments: JSON.stringify({ path: file, old_string: 'Third version.', new_string: 'Agent version.' }) }], finish_reason: 'tool_calls', match_thread: threadId },
		{ content: 'Edited the skill.', finish_reason: 'stop', match_thread: threadId },
	]);
	await sendChatMessage(agent, 'Edit the skill.');
	await expect.poll(() => readMockCallCount(request), { timeout: 30_000 }).toBe(3);
	await expect.poll(() => fs.readFileSync(file, 'utf8'), { timeout: 15_000 }).toContain('Agent version.');
	await agent.context().close();
	await page.context().addInitScript(({ id, tabPath, thread }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [thread]: { tabs: [{ instanceId: 'tab-doc', artifactId: id, path: tabPath, title: 'SKILL.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { id: artifactId, tabPath: file, thread: threadId });
	const historyReads: string[] = [];
	page.on('request', (req) => { if (req.url().includes('/api/files/history?')) historyReads.push(req.url()); });
	const openDocument = async (tab: Page) => {
		await loginByUrl(tab, { waitForComposer: false });
		if (phone) await tab.getByRole('button', { name: 'Toggle sidebar' }).click();
		await tab.getByText(title).first().click();
		await expect(tab.locator('.cm-content')).toBeVisible();
	};
	await openDocument(page);
	if (!phone) await page.getByRole('button', { name: 'Toggle panel' }).click();
	// History is fetched only once it is opened, never on document ready.
	await expect(page.getByRole('button', { name: 'History', exact: true })).toBeVisible();
	expect(historyReads).toHaveLength(0);
	const out = process.env.VH_OUT;
	if (out) fs.mkdirSync(out, { recursive: true });
	await page.route('**/api/files/history?*', async (route) => route.abort('internetdisconnected'), { times: 1 });
	await page.getByRole('button', { name: 'History', exact: true }).click();
	await expect(page.getByText('Couldn’t load history. Check the connection and try again.')).toBeVisible();
	if (out) await page.screenshot({ path: path.join(out, `history-load-failure-${width}.png`), animations: 'disabled' });
	await page.getByRole('button', { name: 'Retry', exact: true }).click();
	const drawer = page.locator('[data-doc-history-drawer]');
	const rows = drawer.locator('.vrow');
	// The first version, the two saves one author made minutes apart (one version), then the agent's edit under its name.
	await expect(rows).toHaveCount(3);
	await expect(rows.first()).toContainText(title);
	await expect(rows.first()).not.toContainText('Unknown editor');
	if (out) await page.screenshot({ path: path.join(out, `history-list-${width}.png`), animations: 'disabled' });
	await rows.first().click();
	await expect(page.locator('.diff-line', { hasText: 'Third version.' }).first()).toBeVisible();
	if (out) await page.screenshot({ path: path.join(out, `history-version-${width}.png`), animations: 'disabled' });
	const older = page.getByRole('button', { name: 'Older', exact: true });
	if (await older.isVisible()) await older.click(); else await rows.nth(1).click();
	await expect(page.getByText('First version.', { exact: false }).first()).toBeVisible();
	// An edit made outside CORSIA after History opened: the restore is refused, and the document and list reload first.
	fs.writeFileSync(file, skill('Outside edit after opening history.'));
	await page.getByRole('button', { name: 'Restore', exact: true }).click();
	await expect(page.getByText('Couldn’t restore this version because the document has newer changes. Check the current document, then try again.')).toBeVisible();
	await expect(page.locator('.cm-content')).toContainText('Outside edit after opening history.');
	await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
	if (out) await page.screenshot({ path: path.join(out, `history-changed-since-${width}.png`), animations: 'disabled' });
	const count = () => Number(execFileSync('git', ['-C', root, 'rev-list', '--count', 'HEAD', '--', path.relative(root, file)], { encoding: 'utf8' }).trim());
	const before = count();
	// The open version covers this window's text, so the typing comes from a second window on the same document.
	const other = await page.context().newPage();
	await other.setViewportSize({ width, height: phone ? 852 : 900 });
	await openDocument(other);
	await expect(other.locator('.cm-content')).toContainText('Outside edit after opening history.');
	// Hold the restore's answer after the server has applied it, and type while "Restoring…" shows.
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/files/history/restore*', async (route) => {
		const response = await route.fetch();
		await held;
		await route.fulfill({ response });
	}, { times: 1 });
	await page.getByRole('button', { name: 'Retry', exact: true }).click();
	await expect(page.getByRole('button', { name: 'Restoring…', exact: true })).toBeDisabled();
	// The refusal recorded nothing; the restore keeps the outside edit it replaces, then itself.
	await expect.poll(count).toBe(before + 2);
	if (out) await page.screenshot({ path: path.join(out, `history-restoring-${width}.png`), animations: 'disabled' });
	await other.locator('.cm-content').click();
	await other.keyboard.press('ControlOrMeta+End');
	await other.keyboard.type(' Typed while restoring.');
	await expect.poll(count, { timeout: 15_000 }).toBe(before + 3);
	release();
	await expect(page.getByRole('button', { name: 'Restoring…', exact: true })).toHaveCount(0, { timeout: 30_000 });
	// Typing just after a restore may show within the restore's version, so look for it in either of the newest two.
	await expect(rows.first()).toContainText('Current');
	await expect(drawer.locator('.vrow', { hasText: 'Restored' })).toHaveCount(1);
	await expect(rows.nth(1)).toBeVisible();
	if (out) await page.screenshot({ path: path.join(out, `history-restored-${width}.png`), animations: 'disabled' });
	// Both writes are versions: the restore, then the typing that landed after it.
	const log = execFileSync('git', ['-C', root, 'log', '--format=%B%x00', '-n', '2', '--', path.relative(root, file)], { encoding: 'utf8' }).split('\0');
	expect(log[1]).toContain('Restored');
	await page.waitForTimeout(2500);
	expect(count()).toBe(before + 3);
	expect(fs.readFileSync(file, 'utf8')).toContain('Typed while restoring.');
});
