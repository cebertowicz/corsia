import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { TEST_TOKEN, createSession, openAppByUrl, openSettings } from './corsia';

export async function seedOutcomes(request: APIRequestContext, label: string) {
	const titles = [`Outcome composition ${label}`, `Passive history ${label}`, `Assigned failures ${label}`];
	const ids = await Promise.all(titles.map(title => createSession(request, title)));
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Use admitted disposable e2e-local mock provisioning');
	const result = execFileSync(python, [path.resolve('e2e/support/focus-outcomes.py'), ...ids], {
		cwd: path.resolve('..'),
		env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
		encoding: 'utf8',
	});
	return { titles, ids, fixture: JSON.parse(result) as { plan: string; live_close: string; live_revision: number; times: string[] } };
}

export async function readStates(request: APIRequestContext, ids: string[]) {
	const response = await request.get('/api/plans/operator/thread-states', {
		params: { ids: ids.join(',') }, headers: { Authorization: `Bearer ${TEST_TOKEN}` },
	});
	expect(response.ok()).toBeTruthy();
	return (await response.json()).states as Record<string, { has_update: boolean; latest_update: { summary: string }; attention_items: { title: string }[] }>;
}

/** The server only acknowledges a displayed chat after its deliberate visible dwell. */
export async function expectObservedNoUpdate(request: APIRequestContext, threadId: string) {
	await expect.poll(async () => (await readStates(request, [threadId]))[threadId].has_update, {
		message: 'the canonical thread-state projection should acknowledge the visible chat',
		timeout: 5_000,
	}).toBe(false);
}

/** Select from the current Activity/sidebar surface without starting another app visit. */
export async function openOutcomeThread(page: Page, title: string) {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.messages')).toBeVisible();
}

export async function focusSetting(page: Page, enabled: boolean) {
	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	const control = page.getByRole('button', { name: 'Focus mode', exact: true });
	if ((await control.getAttribute('aria-pressed') === 'true') !== enabled) await control.click();
	await expect(control).toHaveAttribute('aria-pressed', String(enabled));
	// Phone: the header's one back names the page it returns to ("Settings", SettingsView.svelte); the
	// callers then choose a thread from the sidebar. Desktop: the nav header's close button.
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.locator('.shell-header .h-back').click();
	else await page.getByRole('button', { name: 'Close settings', exact: true }).click();
}

export async function expandLoadedWork(page: Page) {
	// After a thread switch the previous view stays mounted until the selected thread's
	// first page replaces it, so the shell and any fold control prove nothing about
	// the selected thread. Wait for its own first-page content before opening stubs.
	await expect(page.getByText('Meaningful decision update', { exact: true })).toBeVisible();
	// Opening a stub reads its real range. A pause/visit fence may then expose more than one work control.
	for (let round = 0; round < 4; round++) {
		const controls = page.locator('.transcript-fold > .fold-toggle[aria-expanded="false"]');
		if (!await controls.count()) break;
		for (const control of await controls.all()) if (await control.isVisible()) await control.click();
		await expect(page.locator('.fold-state[role="status"]')).toHaveCount(0);
	}
	await expect(page.getByText('Stopped work two', { exact: true })).toBeVisible();
	await expect(page.getByText('New work two', { exact: true })).toBeVisible();
}

export async function assertPassiveGeometry(page: Page) {
	const result = await page.evaluate(() => {
		const describe = (element: Element | null) => element
			? { tag: element.tagName, className: element.className } : null;
		return {
			coarsePointer: matchMedia('(pointer: coarse)').matches,
			input: {
				visible: document.visibilityState,
				focused: document.hasFocus(),
				activeElement: describe(document.activeElement),
				pressedElement: describe(document.querySelector(':active')),
			},
			sections: [...document.querySelectorAll<HTMLElement>('.passive-outcomes')].map(section => {
				const style = getComputedStyle(section);
				const actions = [...section.querySelectorAll<HTMLButtonElement>('button')].filter(button => button.getBoundingClientRect().height > 0);
				const bounds = actions.map(button => {
					const rect = button.getBoundingClientRect();
					const buttonStyle = getComputedStyle(button);
					return {
						className: button.className, top: rect.top, bottom: rect.bottom,
						rectHeight: rect.height, clientHeight: button.clientHeight,
						minHeight: buttonStyle.minHeight, scale: buttonStyle.scale, active: button.matches(':active'),
					};
				});
				return {
					background: style.backgroundColor, border: style.borderTopWidth, radius: style.borderRadius, actions: bounds,
					overlap: bounds.some((rect, i) => bounds.some((other, j) => j > i && rect.top < other.bottom && other.top < rect.bottom)),
					overflow: section.scrollWidth > section.clientWidth,
					timeInsideActions: actions.filter(button => button.classList.contains('row')).every(button => Boolean(button.querySelector('time'))),
				};
			}),
		};
	});
	const diagnostic = JSON.stringify(result);
	for (const section of result.sections) {
		expect(section.background, diagnostic).toBe('rgba(0, 0, 0, 0)');
		expect(section.border, diagnostic).toBe('0px'); expect(section.radius, diagnostic).toBe('0px');
		// PassiveOutcomeList.svelte: the toggle is always 44px; node rows are 32px, 44px under `(pointer: coarse)`.
		const target = (action: { className: string }) => action.className.includes('passive-toggle') || result.coarsePointer ? 44 : 32;
		expect(section.actions.every(action => action.rectHeight >= target(action)), diagnostic).toBe(true);
		expect(section.overlap, diagnostic).toBe(false); expect(section.overflow, diagnostic).toBe(false); expect(section.timeInsideActions, diagnostic).toBe(true);
	}
	expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), diagnostic).toBe(true);
}

/** Verify the actual sidebar preview and the user-assigned failure count. */
export async function assertOutcomeSidebar(page: Page, titles: string[]) {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	const passiveRow = page.locator('.thread-item').filter({ hasText: titles[1] });
	await expect(passiveRow).toContainText('Closed: Passive sidebar snapshot');
	await expect(passiveRow.locator('.update-dot')).toHaveCount(0);
	const failedRow = page.locator('.thread-item').filter({ hasText: titles[2] });
	await expect(failedRow.getByRole('button', { name: 'Failed: 1 item', exact: true })).toBeVisible();
}

/** Check the composed sibling order after real work ranges have loaded. */
export async function assertOutcomeOrder(page: Page) {
	const order = await page.locator('.messages').evaluate(root => {
		const nodes = [...root.querySelectorAll('.transcript-fold, .passive-outcomes, .plan-limit-paused, [data-visit-divider], [data-message-id]')];
		return nodes.filter(node => !node.closest('.fold-details') || node.matches('[data-message-id]')).map(node => ({ text: node.textContent, pause: node.matches('.plan-limit-paused'), divider: node.matches('[data-visit-divider]'), passive: node.matches('.passive-outcomes') }));
	});
	const stopped = order.findIndex(row => row.text?.includes('Stopped work two'));
	const pause = order.findIndex(row => row.pause);
	const next = order.findIndex(row => row.text?.includes('Work after pause'));
	const divider = order.findIndex(row => row.divider);
	const newWork = order.findIndex(row => row.text?.includes('New work one'));
	expect([stopped, pause, next, divider, newWork].every(index => index >= 0)).toBe(true);
	const lists = order.flatMap((row, index) => row.passive ? [index] : []);
	expect(lists).toHaveLength(4);
	expect(stopped).toBeLessThan(lists[0]); expect(lists[0]).toBeLessThan(pause);
	expect(pause).toBeLessThan(next); expect(next).toBeLessThan(lists[1]); expect(lists[1]).toBeLessThan(divider);
	expect(divider).toBeLessThan(newWork); expect(newWork).toBeLessThan(lists[2]); expect(lists[2]).toBeLessThan(lists[3]);
}

export async function appendClosure(request: APIRequestContext, fixture: { plan: string; live_close: string; live_revision: number }) {
	const response = await request.post(`/api/plans/${fixture.plan}/mutations`, {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` },
		data: { operation: 'close_node', mutation_id: crypto.randomUUID(), params: { node_id: fixture.live_close, expected_revision: fixture.live_revision, reason: 'Synthetic outcome proof' } },
	});
	expect(response.ok(), await response.text()).toBeTruthy();
}
