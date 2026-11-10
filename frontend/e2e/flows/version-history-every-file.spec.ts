import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import type { APIRequestContext, Page, TestInfo } from '@playwright/test';
import { test, expect } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

const authQuery = `?${['to', 'ken'].join('')}=${encodeURIComponent(TEST_TOKEN)}`;
const FIRST = 'Plan for the week.\nKeep this line.\nDelete this line.\n';
const EDITS = ['Plan for the week.\nKeep this line.\n', 'Plan for the week.\nKeep this line.\nAdded line.\n'];

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

async function openHistory(page: Page, info: TestInfo, title: string, artifactId: string, file: string, threadId: string) {
	const phone = info.project.name === 'chromium-mobile';
	await page.context().addInitScript(({ id, tabPath, thread, name }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [thread]: { tabs: [{ instanceId: 'tab-doc', artifactId: id, path: tabPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'tab-doc', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { id: artifactId, tabPath: file, thread: threadId, name: path.basename(file) });
	await loginByUrl(page, { waitForComposer: false });
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title).first().click();
	await expect(page.locator('.cm-content')).toBeVisible();
	if (!phone) await page.getByRole('button', { name: 'Toggle panel' }).click();
	await page.getByRole('button', { name: 'History', exact: true }).click();
	return page.locator('[data-doc-history-drawer]');
}

function shots(width: number) {
	const out = process.env.VH_OUT;
	if (out) fs.mkdirSync(out, { recursive: true });
	return async (page: Page, name: string) => {
		if (out) await page.screenshot({ path: path.join(out, `${name}-${width}.png`), animations: 'disabled' });
	};
}

test('@flow:version-history-every-file a workspace document shows deleted text, steps and restores', async ({ page, request }, info) => {
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	const shot = shots(width);
	const title = `history-doc-${width}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = await workspaceRoot(request);
	const file = path.join(root, 'docs', `plan-${width}-${Date.now()}.md`);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, FIRST);
	// An ordinary document is recorded from its first save: the text it had, then each save.
	for (const text of EDITS) await save(request, file, text, threadId);
	await expect.poll(() => commits(root, file)).toBe(3);
	const drawer = await openHistory(page, info, title, await register(request, file, threadId), file, threadId);
	const rows = drawer.locator('.vrow');
	// The two saves a minute apart by one author show as one version.
	await expect(rows).toHaveCount(2);
	await expect(rows.first()).toContainText('Current');
	// A version shows its changes from the one before it, so the newest shows the deleted line.
	await rows.first().click();
	await expect(page.locator('.diff-line.del')).toContainText('Delete this line.');
	await expect(page.locator('.diff-line.add')).toContainText('Added line.');
	await shot(page, 'history-doc-deleted-text');
	const older = page.getByRole('button', { name: 'Older', exact: true });
	if (await older.isVisible()) await older.click(); else await rows.nth(1).click();
	await expect(page.locator('.diff-line.del')).toHaveCount(0);
	await expect(page.locator('.diff-line.add', { hasText: 'Delete this line.' })).toBeVisible();
	await shot(page, 'history-doc-older-version');
	await page.getByRole('button', { name: 'Restore', exact: true }).click();
	await expect(page.getByRole('button', { name: 'Restoring…', exact: true })).toHaveCount(0, { timeout: 30_000 });
	await expect(rows).toHaveCount(3);
	await expect(rows.first()).toContainText('Current');
	await expect(rows.first()).toContainText('Restored');
	await expect(page.locator('.cm-content')).toContainText('Delete this line.');
	expect(fs.readFileSync(file, 'utf8')).toBe(FIRST);
	await shot(page, 'history-doc-restored');
});

test('@flow:version-history-every-file a registered chat file has history; an unregistered one does not', async ({ page, request }, info) => {
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	const shot = shots(width);
	const title = `history-tray-${width}-${Date.now()}`;
	const threadId = await createSession(request, title);
	const root = await workspaceRoot(request);
	const dir = path.join(root, '.threads', threadId);
	fs.mkdirSync(dir, { recursive: true });
	const kept = path.join(dir, `notes-${width}.md`);
	const loose = path.join(dir, `scratch-${width}.md`);
	fs.writeFileSync(kept, FIRST);
	fs.writeFileSync(loose, FIRST);
	const artifactId = await register(request, kept, threadId);
	for (const text of EDITS) await save(request, kept, text, threadId);
	await save(request, loose, EDITS[0], null);
	await expect.poll(() => commits(root, kept)).toBe(3);
	// Git ignores chat folders; the registered file is recorded anyway, the unregistered one never.
	expect(execFileSync('git', ['-C', root, 'check-ignore', '--no-index', '--', path.relative(root, kept)], { encoding: 'utf8' }).trim()).toBe(path.relative(root, kept));
	await page.waitForTimeout(1000);
	expect(commits(root, loose)).toBe(0);
	const drawer = await openHistory(page, info, title, artifactId, kept, threadId);
	const rows = drawer.locator('.vrow');
	await expect(rows).toHaveCount(2);
	await rows.first().click();
	await expect(page.locator('.diff-line.del')).toContainText('Delete this line.');
	await shot(page, 'history-tray-file');
});
