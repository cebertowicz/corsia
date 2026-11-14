import fs from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { e2eModel, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { createProject } from '../support/measurement-fixture';

/**
 * A path an agent writes in a project chat, counted from the workspace root, becomes a reference pill that opens the
 * file in the pane in one press, even unregistered and in another thread's folder. A path with no file stays text.
 * A chat link that can't open says why in its own tab with nothing over it; a link with no tab to say it still toasts.
 */

async function evidence(page: Page, name: string): Promise<void> {
	const directory = process.env.CORSIA_E2E_EVIDENCE_DIR;
	if (!directory) return;
	fs.mkdirSync(directory, { recursive: true });
	await page.screenshot({ path: path.join(directory, `${name}.png`), animations: 'disabled' });
}

/** A new project, its folder and the workspace root. */
async function newProject(request: APIRequestContext, label: string) {
	const stamp = Date.now();
	const project = await createProject(request, `${label} ${stamp}`);
	const tree = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN, nodes: 'false', project_id: project.id } })).json();
	const planning = (tree.roots as Array<{ id: string; path: string }>).find((root) => root.id === 'planning')!.path;
	const workspace = (await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN, nodes: 'false' } })).json())
		.roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	return { stamp, projectId: project.id as string, planning, workspace };
}

/** Opens a project chat holding one agent message, the way a person reaches it: from a link in a workspace chat. */
async function openChatMessage(page: Page, request: APIRequestContext, phone: boolean, projectId: string, stamp: number, content: string): Promise<Locator> {
	const created = await request.post('/api/threads', { params: { token: TEST_TOKEN }, data: { title: `Pills chat ${stamp}`, project_id: projectId, model: e2eModel() } });
	const thread = (await created.json()).thread_id as string;
	const message = await injectMessage(request, thread, { role: 'assistant', content, metadata: { for_user: true } }, TEST_TOKEN);
	const hub = await request.post('/api/threads', { params: { token: TEST_TOKEN }, data: { title: `Pills hub ${stamp}`, model: e2eModel() } });
	await injectMessage(request, (await hub.json()).thread_id as string, {
		role: 'assistant', content: `Go to [the project chat](/thread/${thread}).`, metadata: { for_user: true },
	}, TEST_TOKEN);

	await page.setViewportSize(phone ? { width: 393, height: 852 } : { width: 1440, height: 900 });
	await openAppPastLanding(page);
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, `Pills hub ${stamp}`).click();
	await waitForWsConnected(page);
	await page.locator('.md-content a', { hasText: 'the project chat' }).click();
	const row = page.locator(`[data-message-id="${message}"]`);
	await expect(row).toBeVisible({ timeout: 15_000 });
	return row;
}

test('@surface:chat-cards a workspace-relative path in a project chat opens in one press; a missing one stays text', async ({ page, request }, info) => {
	const phone = info.project.name.includes('mobile');
	const { stamp, projectId, planning, workspace } = await newProject(request, 'Path pills');
	fs.mkdirSync(path.join(planning, '.threads', 'other'), { recursive: true });
	fs.writeFileSync(path.join(planning, '.threads', 'other', 'report.md'), '# Report\n\nThe other thread wrote this.\n');
	const fromWorkspace = path.relative(workspace, planning);
	const row = await openChatMessage(page, request, phone, projectId, stamp,
		`The report is at ${fromWorkspace}/.threads/other/report.md and the draft at ${fromWorkspace}/.threads/other/none.md.`);

	const pill = row.locator('.cards').getByText('report.md');
	await expect(pill).toBeVisible();
	await expect(row.locator('.cards').getByText('none.md')).toHaveCount(0);
	await expect(row.locator('.md-content')).toContainText(`${fromWorkspace}/.threads/other/none.md`);
	await expect(row.locator('.md-content a', { hasText: 'none.md' })).toHaveCount(0);
	await evidence(page, `chat-path-pill-${phone ? 393 : 1440}-mocked`);

	await pill.click();
	await expect(page.locator('.cm-content').filter({ hasText: 'The other thread wrote this.' })).toBeVisible({ timeout: 15_000 });
	await expect(page.locator('.load-error')).toHaveCount(0);
	await evidence(page, `chat-path-pill-opened-${phone ? 393 : 1440}-mocked`);
});

test('@surface:chat-cards a chat link that cannot open says why in its own tab with no toast; a link with no tab still toasts', async ({ page, request }, info) => {
	const phone = info.project.name.includes('mobile');
	const width = phone ? 393 : 1440;
	const { stamp, projectId, planning, workspace } = await newProject(request, 'Link failures');
	fs.mkdirSync(path.join(planning, 'notes'), { recursive: true });
	fs.writeFileSync(path.join(planning, 'notes', 'proposal.md'), '# Proposal\n\nBody.\n');
	const row = await openChatMessage(page, request, phone, projectId, stamp,
		'The proposal: [Proposal](notes/proposal.md). Also [Missing](notes/missing.md), [Elsewhere](notes/elsewhere.md) and [Old copy](ftp://files.example.invalid/proposal.md).');
	const press = (text: string) => row.locator('.md-content a', { hasText: new RegExp(`^${text}$`) }).click();
	const documentToasts = page.getByRole('status').filter({ hasText: 'linked workspace document' });

	// No file at the path: its tab says so, and nothing covers that line.
	await press('Missing');
	const unavailable = page.getByText('missing.md is unavailable.');
	await expect(unavailable).toBeInViewport();
	await expect(page.getByText('There’s no file at this link.')).toBeInViewport();
	await expect(documentToasts).toHaveCount(0);
	expect(await unavailable.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const top = document.elementFromPoint(box.left + 4, box.top + box.height / 2);
		return !!top && el.contains(top);
	})).toBe(true);
	await evidence(page, `chat-link-missing-${width}-mocked`);
	await page.getByRole('button', { name: 'Close tab', exact: true }).click();
	await expect(row).toBeVisible();

	// A destination no tab opens for: the toast is the only place to say it.
	await press('Old copy');
	const unsupported = page.getByRole('status').filter({ hasText: 'This link uses an unsupported destination' });
	await expect(unsupported).toBeVisible();
	await expect(page.getByText('proposal.md is unavailable.')).toHaveCount(0);
	if (!phone) await expect(page.getByRole('tab')).toHaveCount(0);
	await evidence(page, `chat-link-unsupported-toast-${width}-mocked`);
	await unsupported.getByRole('button').click();
	await expect(unsupported).toHaveCount(0);

	// The file's read fails: its tab shows the error with Retry, and nothing covers that line.
	await page.route(/\/api\/files\/revision/, (route) => route.fulfill({ status: 500, body: 'read failed' }));
	await press('Proposal');
	const failed = page.getByText("Couldn't open proposal.md.");
	await expect(failed).toBeInViewport();
	await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
	await expect(documentToasts).toHaveCount(0);
	expect(await failed.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const top = document.elementFromPoint(box.left + 4, box.top + box.height / 2);
		return !!top && el.contains(top);
	})).toBe(true);
	await evidence(page, `chat-link-error-${width}-mocked`);

	// No file under the project, and the read from the workspace root fails: a failed read with Retry, never 'no file'.
	await page.unroute(/\/api\/files\/revision/);
	const fromWorkspaceRoot = path.join(workspace, 'notes', 'elsewhere.md');
	await page.route(/\/api\/files\/revision/, (route) => (
		new URL(route.request().url()).searchParams.get('path') === fromWorkspaceRoot
			? route.fulfill({ status: 500, body: 'read failed' }) : route.continue()));
	// On a phone the open tab covers the chat; Chat hands the canvas back, with the tab kept.
	if (phone) await page.getByRole('button', { name: 'Chat', exact: true }).click();
	await press('Elsewhere');
	const elsewhere = page.getByText("Couldn't open elsewhere.md.");
	await expect(elsewhere).toBeInViewport();
	await expect(elsewhere.locator('..').getByRole('button', { name: 'Retry' })).toBeVisible();
	await expect(page.getByText('There’s no file at this link.')).toHaveCount(0);
	await expect(documentToasts).toHaveCount(0);
	await evidence(page, `chat-link-other-root-error-${width}-mocked`);
});
