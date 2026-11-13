import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { shoot } from '../support/comment-count-probe';

const repo = path.resolve(process.cwd(), '..');

// A phone hides the menu, so the controls that open it carry the rows' needs-you dot: the header's
// Toggle sidebar while the menu is closed, the New chat button while it is open. Dismiss all clears both.
test('a phone menu carries the needs-you dot until Dismiss all', async ({ page, request }, testInfo) => {
	test.skip(!testInfo.project.use.hasTouch, 'Phone layout only');
	test.setTimeout(240_000);
	await page.setViewportSize({ width: 393, height: 900 });
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const title = `Menu dot ${Date.now()}`;
	const threadId = await createSession(request, title);
	const docFolder = path.join(workspace, 'comment-task-menu-dot');
	mkdirSync(docFolder, { recursive: true });
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const saved = await request.put(`/api/files/content?${query}`, { data: { path: path.join(docFolder, 'Launch plan.md'), text: 'Launch plan details for review.', thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', 'feedback', artifactId, threadId], {
		cwd: repo, encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});

	const toggle = (name: string) => page.getByRole('button', { name, exact: true });
	const newChat = page.locator('button.rail-newchat');
	const toggleDot = page.getByTestId('menu-needs-you-dot');
	const fabDot = page.getByTestId('fab-needs-you-dot');

	// Menu closed: the toggle carries the dot.
	await openAppPastLanding(page);
	await expect(toggle('Toggle sidebar · Needs you')).toBeVisible();
	await expect(toggleDot).toBeVisible();
	await expect(toggleDot).toHaveCSS('width', '8px');
	await shoot(page, testInfo, '393-menu-closed-dot');

	// Menu open: the New chat button carries it, beside the row's own mark.
	await toggle('Toggle sidebar · Needs you').click();
	const row = page.locator('aside.sidebar').getByRole('listitem').filter({ hasText: title });
	await expect(row.getByRole('button', { name: /Needs you/ })).toBeVisible();
	await expect(newChat).toBeVisible();
	await expect(newChat).toHaveAttribute('aria-label', 'New chat · Needs you');
	await expect(fabDot).toBeVisible();
	await shoot(page, testInfo, '393-menu-open-dot');

	// Dismiss all from the file task's row menu (long-press), as a phone does.
	await sidebarThread(page, title).click();
	const chat = page.getByRole('button', { name: 'Chat', exact: true });
	await expect(async () => {
		if (await chat.isVisible()) await chat.click({ timeout: 2000 });
		await page.getByRole('button', { name: /^Open plan —/ }).click({ timeout: 2000 });
		await expect(page.getByRole('dialog', { name: /^Plan —/ })).toBeVisible({ timeout: 2000 });
	}).toPass({ timeout: 30_000 });
	const taskRow = page.getByRole('list', { name: 'Plan steps' }).getByRole('listitem')
		.filter({ hasText: 'Launch plan.md' }).filter({ has: page.locator('.count-text') }).first();
	await expect(taskRow).toContainText('2 comments, 2 suggestions');
	const box = await taskRow.boundingBox();
	if (!box) throw new Error('Task row has no bounds');
	const session = await page.context().newCDPSession(page);
	const touchPoints = [{ x: box.x + box.width / 2, y: box.y + box.height / 2 }];
	await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints });
	await page.waitForTimeout(600);
	await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	await session.detach();
	await page.getByRole('menuitem', { name: 'Dismiss all' }).click();

	// Nothing needs the person: the live page drops both dots, and a fresh load shows neither.
	await expect(toggleDot).toHaveCount(0);
	await expect(fabDot).toHaveCount(0);
	await openAppPastLanding(page);
	await expect(toggle('Toggle sidebar')).toBeVisible();
	await expect(toggleDot).toHaveCount(0);
	await shoot(page, testInfo, '393-menu-closed-clear');
	await toggle('Toggle sidebar').click();
	await expect(row).toBeVisible();
	await expect(row.getByRole('button', { name: /Needs you/ })).toHaveCount(0);
	await expect(newChat).toBeVisible();
	await expect(newChat).toHaveAttribute('aria-label', 'New chat');
	await expect(fabDot).toHaveCount(0);
	await shoot(page, testInfo, '393-menu-open-clear');
});
