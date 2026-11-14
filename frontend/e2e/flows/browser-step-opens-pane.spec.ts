import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, sendChatMessage, sidebarThread, saveEvidenceScreenshot } from '../support/corsia';
import { seedMockResponses } from '../support/mock';
import { startProbeServer } from '../support/probeServer';

// A sign-in page the agent reached in its own (headless) browser asks the person for their password. One press on
// the step, from the chat chip, the plan row or the sidebar's needs-you list, shows that browser in the pane in front.

const chip = (page: Page) => page.getByRole('button', { name: /Do: Enter your password in the shared browser/ });
const canvas = (page: Page) => page.locator('canvas[aria-label="Shared browser"]');

/** The page's pixels reached the pane: the probe's password field paints its top-left corner. */
async function expectPagePixels(page: Page): Promise<void> {
	await expect(canvas(page)).toBeVisible({ timeout: 30_000 });
	await expect.poll(() => canvas(page).evaluate(node => {
		const frame = node as HTMLCanvasElement;
		return Array.from(frame.getContext('2d')!.getImageData(5, 5, 1, 1).data).slice(0, 3);
	}), { timeout: 20_000 }).toEqual([240, 240, 240]);
}

/** The agent opens the sign-in page headless and reads it, which raises the password step in `thread`'s plan. */
async function reachPasswordPage(page: Page, request: APIRequestContext, thread: string, origin: string): Promise<void> {
	await seedMockResponses(request, [
		{ tool_calls: [{ id: 'open-headless', name: 'browser_open', arguments: JSON.stringify({ url: `${origin}/browser-pane-probe.html?password` }) }], finish_reason: 'tool_calls', match_thread: thread },
		{ tool_calls: [{ id: 'read-page', name: 'browser_snapshot', arguments: JSON.stringify({}) }], finish_reason: 'tool_calls', match_thread: thread },
		{ content: 'Waiting for your password.', finish_reason: 'stop', match_thread: thread },
	]);
	await sendChatMessage(page, 'Sign in to the fixture.');
	await expect(chip(page)).toBeVisible({ timeout: 45_000 });
	// Nothing showed the agent's browser on its own: the step is what reveals it.
	await expect(canvas(page)).toHaveCount(0);
}

async function closeBrowserTab(page: Page): Promise<void> {
	await page.getByRole('button', { name: /^Close .*browser-pane-probe/ }).click();
	await expect(canvas(page)).toHaveCount(0);
}

test('one press on the sign-in chip shows the agent’s browser', async ({ page, request }, info) => {
	test.setTimeout(150_000);
	const phone = info.project.name === 'chromium-mobile';
	if (phone) await page.setViewportSize({ width: 393, height: 852 });
	const width = phone ? 393 : 1440;
	const probe = await startProbeServer();
	try {
		const title = `sign-in-chip-${Date.now()}`;
		const thread = await createSession(request, title);
		await loginByUrl(page);
		if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		await reachPasswordPage(page, request, thread, probe.origin);
		await saveEvidenceScreenshot(page, info, `${width}-1-chip`, { fullPage: false });

		await (phone ? chip(page).tap() : chip(page).click());
		await saveEvidenceScreenshot(page, info, `${width}-2-pressed`, { fullPage: false });
		await expectPagePixels(page);
		await saveEvidenceScreenshot(page, info, `${width}-3-browser`, { fullPage: false });
		if (phone) return;

		// The plan row opens the same browser.
		await closeBrowserTab(page);
		const planRow = page.getByRole('list', { name: 'Plan steps' }).getByRole('listitem').filter({ hasText: 'Enter your password' }).first();
		await planRow.locator('button.row-body').first().click();
		await expectPagePixels(page);
		await saveEvidenceScreenshot(page, info, `${width}-4-plan-row`, { fullPage: false });

		// So does the step in the sidebar's needs-you list.
		await closeBrowserTab(page);
		const row = page.locator('aside.sidebar .thread-item').filter({ hasText: title });
		await row.getByRole('button', { name: /: 1 item$/ }).click();
		await page.getByRole('dialog').getByRole('button', { name: /Enter your password/ }).click();
		await expectPagePixels(page);
		await saveEvidenceScreenshot(page, info, `${width}-5-needs-you`, { fullPage: false });
	} finally { await probe.close(); }
});

test('a subagent’s sign-in step opens the subagent’s browser from the parent’s plan', async ({ page, request }, info) => {
	test.skip(info.project.name === 'chromium-mobile', 'desktop proof');
	test.setTimeout(150_000);
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const probe = await startProbeServer();
	try {
		const stamp = Date.now();
		const parentTitle = `sign-in-parent-${stamp}`;
		const childTitle = `sign-in-child-${stamp}`;
		const parent = await createSession(request, parentTitle);
		const child = await createSession(request, childTitle);
		const streams: string[] = [];
		const sent: string[] = [];
		page.on('websocket', socket => {
			if (!socket.url().includes('/api/browser/')) return;
			streams.push(new URL(socket.url()).pathname);
			socket.on('framesent', frame => { if (typeof frame.payload === 'string') sent.push(`${new URL(socket.url()).pathname} ${frame.payload.slice(0, 80)}`); });
		});
		await loginByUrl(page);
		await sidebarThread(page, childTitle).click();
		await reachPasswordPage(page, request, child, probe.origin);

		// The child becomes the parent's subagent behind one delegated step.
		execFileSync(python, ['frontend/e2e/support/browser-step-child-seed.py', parent, child, 'Sign in to the fixture'], {
			cwd: path.resolve(process.cwd(), '..'), encoding: 'utf8',
			env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
		});
		await page.reload();
		await sidebarThread(page, parentTitle).click();
		const plan = page.getByRole('list', { name: 'Plan steps' }).first();
		await plan.getByRole('listitem').filter({ hasText: 'Sign in to the fixture' }).getByRole('button', { name: /subagent/ }).click();
		const nested: Locator = plan.getByRole('listitem').filter({ hasText: 'Enter your password' }).first();
		await expect(nested).toBeVisible({ timeout: 20_000 });
		await saveEvidenceScreenshot(page, info, 'nested-1-plan', { fullPage: false });
		await nested.locator('button.row-body').first().click();
		await expectPagePixels(page);
		// The pane streams the subagent's session, not the parent's. On a password page the
		// agent's mark stays hidden, as for any session's password page.
		expect(streams).toContain(`/api/browser/${child}/stream`);
		expect(streams).not.toContain(`/api/browser/${parent}/stream`);
		await expect(page.locator('.agent-seam')).not.toHaveClass(/present/);
		await saveEvidenceScreenshot(page, info, 'nested-2-browser', { fullPage: false });

		// The person's own input goes to the subagent's page: their password signs it in.
		const box = (await canvas(page).boundingBox())!;
		await canvas(page).click({ position: { x: box.width / 2, y: box.height * 0.25 } });
		await page.keyboard.press('Meta+A');
		await page.keyboard.type('next');
		await page.keyboard.press('Enter');
		await expect.poll(() => canvas(page).evaluate(node => {
			const frame = node as HTMLCanvasElement;
			return Array.from(frame.getContext('2d')!.getImageData(frame.width / 2, frame.height / 2, 1, 1).data).slice(0, 3);
		}), { timeout: 20_000 }).toEqual([0, 160, 80]);
		expect(sent.some(entry => entry.startsWith(`/api/browser/${child}/stream`))).toBe(true);
		expect(sent.some(entry => entry.startsWith(`/api/browser/${parent}/stream`))).toBe(false);
		await saveEvidenceScreenshot(page, info, 'nested-3-signed-in', { fullPage: false });
	} finally { await probe.close(); }
});
