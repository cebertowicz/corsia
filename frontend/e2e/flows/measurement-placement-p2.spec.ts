import { expect, test, type APIRequestContext } from '@playwright/test';
import { e2eModel, loginByUrl, openSettings, TEST_TOKEN } from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);

/**
 * PL-1..PL-4 and PL-7 on the composed shell: the rail destination, the header
 * doorway from a lane and from a conductor, the Project Activity doorway, the
 * Close path back to the doorway the reader came from, and the Settings entry
 * that must no longer exist.
 *
 * Every action names its accessible name rather than a CSS hook, because the
 * claim being proved is that a person can find and leave the surface. The
 * measurement endpoints are served from this file so the scope label and the
 * triage list depend on the click path and not on how much history the test
 * account happens to carry.
 */

async function createProject(request: APIRequestContext, name: string) {
	const response = await request.post(`/api/projects?token=${tok}`, {
		data: { name, description: 'Synthetic Measurement placement fixture', status: 'active', accent_color: '#d65a87' },
	});
	expect(response.ok()).toBeTruthy();
	const projects = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
	const project = (projects.projects || []).find((candidate: { name: string }) => candidate.name === name);
	expect(project, 'the seeded project is available to the navigation palette').toBeTruthy();
	return project as { id: string; name: string };
}

async function createAgent(request: APIRequestContext, title: string, projectId: string) {
	const created = await request.post(`/api/threads?token=${tok}`, {
		data: { title, project_id: projectId, model: e2eModel() },
	});
	expect(created.ok()).toBeTruthy();
	const { thread_id: threadId } = await created.json() as { thread_id: string };
	const promoted = await request.patch(`/api/agents/${threadId}?token=${tok}`, {
		data: { responsibility: `Synthetic Measurement lane for ${title}`, active: true },
	});
	expect(promoted.ok()).toBeTruthy();
	return threadId;
}

const column = (value: number | null) => ({ value, n: 20, minimum_n: 20, periods: [] });
const rule = (flagged: boolean) => ({
	eligible: true, metric: 'fresh_tokens_per_outcome', minimum_n: 20, flagged,
	current: 140, mean: 100, sd: 12, standardized_size: 3.3, zero_variance_baseline: false,
	relative_adverse_change: 0.4, action: 'Review its costliest recent tasks.', window: 'trailing 8 weeks',
});

/**
 * The Agents payload the surface reads its scope label and project rows from.
 * One clean row for the fixture's own agent, so the triage list and the picker
 * have a project and an agent to name, and nothing is flagged.
 *
 * The envelope echoes the request it answers: the surface's own contract rejects
 * an answer whose scope, grain, order or metric names a selection the reader is
 * not looking at, and 'none' is how no-agent travels in a query string.
 */
function agentsBody(project: { id: string; name: string }, agentId: string, agentName: string, request: URL) {
	const asked = request.searchParams.get('agent');
	return {
		definition_version: 1,
		scope: {
			project: request.searchParams.get('project') ?? 'all',
			agent: asked && asked !== 'none' ? asked : null,
		},
		sort: request.searchParams.get('sort') ?? 'ranked',
		metric: request.searchParams.get('metric') ?? 'fresh_tokens_per_outcome',
		period: { grain: request.searchParams.get('grain') ?? 'days' },
		caption: 'Trailing 8 completed weeks',
		findings_state: 'healthy',
		findings: [],
		project_summaries: [{ project: { id: project.id, name: project.name }, flagged_agents: 0, outcomes: 20 }],
		inactive_projects: [],
		rows: [
			{
				agent: { id: agentId, name: agentName, project: { id: project.id, name: project.name } },
				n: 20,
				difficulty_mix: { standard: 20 },
				status: 'eligible',
				rank: 1,
				selected_metric: 'fresh_tokens_per_outcome',
				metrics: {
					outcomes: column(20), fresh_tokens_per_outcome: column(140),
					first_try_acceptance: column(0.7), asks_per_outcome: column(0.1),
					active_hours_per_outcome: { ...column(0.05), previous_value: 0.1, previous_n: 20 },
				},
				worsening: rule(false),
				flags: { fresh_tokens_per_outcome: rule(false), first_try_acceptance: rule(false) },
			},
		],
	};
}

async function serveMeasurement(page: import('@playwright/test').Page, project: { id: string; name: string }, agentId: string, agentName: string) {
	await page.route('**/api/measurement/**', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/agents')) {
			await route.fulfill({ json: agentsBody(project, agentId, agentName, url) });
			return;
		}
		if (url.pathname.endsWith('/models')) {
			await route.fulfill({ json: {
				rows: [
					{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', tasks: 20, failed_turns_per_100: null, recorded_cost_per_task: 0.5, cost_class: 'priced', rate_reason: 'none', cost_change: null, change_reason: 'new', previous_tasks: 0, not_offered_now: false, other: false, includes_local: false, recording_gap: false },
					{ model: 'codex/gpt-6.1-sol', label: 'gpt-6.1-sol', tasks: 14, failed_turns_per_100: 1.2, recorded_cost_per_task: 1.6, cost_class: 'priced', rate_reason: 'none', cost_change: -0.1, change_reason: 'none', previous_tasks: 18, not_offered_now: false, other: false, includes_local: false, recording_gap: false },
				],
				token_shares: [{ model: 'claude-code/claude-opus-5-5', label: 'Claude Opus 5.5', share_of_tokens: 0.75 }, { model: 'codex/gpt-6.1-sol', label: 'gpt-6.1-sol', share_of_tokens: 0.25 }],
				min_tasks: 14, unmatched_failed_turns: 0, unresolved_token_share: 0, total_spend_usd: 0,
			} });
			return;
		}
		const ids = url.searchParams.get('ids')?.split(',').filter(Boolean) ?? [];
		await route.fulfill({ json: {
			rows: ids.flatMap((series) => ['2026-08-03', '2026-08-10'].map((period) => ({
				period, series, value: series === 'outcomes' ? 20 : 1, n: 20, proxy: false,
				partial: false, comparable: true, break: null, dimension: null, thin: false,
			}))),
			definitions: [],
		} });
	});
}

/**
 * Below 768px the rail is a drawer over the page: open it to act on the rail,
 * close it to act on what the rail opened. The app gives no inspectable marker
 * for that state (the sidebar keeps a width at every state), so the flow keeps
 * its own: a fresh page starts closed, and every toggle here moves it.
 */
const railIsOpen = new WeakMap<object, boolean>();

async function setRail(page: import('@playwright/test').Page, wantOpen: boolean) {
	if ((page.viewportSize()?.width ?? 0) > 768) return;
	if ((railIsOpen.get(page) ?? false) === wantOpen) return;
	await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	railIsOpen.set(page, wantOpen);
}

const openRail = (page: import('@playwright/test').Page) => setRail(page, true);

/**
 * Choosing a rail destination dismisses the drawer by itself, so the page the
 * reader just opened is the page they act on. Record that; toggling again would
 * put the drawer back over it.
 */
function railDismissed(page: import('@playwright/test').Page): void {
	railIsOpen.set(page, false);
}

test.describe('@flow:measurement-placement-p2 @surface:navigation place Measurement in navigation (PL-1..PL-4, PL-7)', () => {
	test('the rail destination opens All work as a triage list, with no charts and no second home', async ({ page, request }, testInfo) => {
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);
		await loginByUrl(page, { waitForComposer: false });

		await openRail(page);
		await page.getByRole('link', { name: 'Measurement', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);

		await expect(page.locator('.shell-header .h-title')).toHaveText('All work');
		// PL-1: a first visit is All work, and it carries every chart; the scope select is the route to a project.
		await expect(page.getByRole('button', { name: 'Measurement scope: All work', exact: true })).toBeVisible();
		await expect(page.locator('.project-list')).toHaveCount(0);
		// Cost carries the workspace bill and what was paid, which belong to All work.
		await expect(page.locator('section.cost')).toBeVisible();

		// PL-2 fallback: a rail visit has no doorway behind it, so Close lands on
		// Activity rather than inventing a thread to go back to.
		await page.getByRole('button', { name: 'Close Measurement', exact: true }).click();
		await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
	});

	test('Settings offers no second home for Measurement (PL-7)', async ({ page, request }, testInfo) => {
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		await serveMeasurement(page, project, 'agent-none', 'No agent');
		await loginByUrl(page, { waitForComposer: false });

		await openSettings(page);
		// Scoped to the settings surface: the rail destination is a button with this
		// name everywhere, and PL-7 removes only the second home inside Settings.
		await expect(page.getByRole('main').getByRole('link', { name: 'Measurement', exact: true })).toHaveCount(0);
		await expect(page.locator('.measurement-page')).toHaveCount(0);
		await expect(page.getByRole('heading', { name: 'Measurement', exact: true })).toHaveCount(0);
	});

	test('the scope picker is searchable, keyboard-closable, and stays inside the viewport', async ({ page, request }, testInfo) => {
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);
		await loginByUrl(page, { waitForComposer: false });

		await openRail(page);
		await page.getByRole('link', { name: 'Measurement', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await page.getByRole('button', { name: /Measurement scope:/ }).click();

		const input = page.getByRole('combobox', { name: 'Search projects and agents' });
		await expect(input).toBeVisible();
		await input.fill(agentTitle);
		// PL-5: agents are listed under their project, so narrowing to a name shows both.
		await expect(page.getByRole('option', { name: new RegExp(agentTitle) })).toBeVisible();

		// Regression proof: the popover never escapes the viewport it is opened in.
		// Dev's mobile project is a 412px Pixel 7 rather than the 393px case the
		// placement note named, so the bound is the live viewport width.
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			const box = await page.locator('.picker-panel').boundingBox();
			expect(box).not.toBeNull();
			expect(box!.x).toBeGreaterThanOrEqual(0);
			expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()?.width ?? 0);
		}

		await page.keyboard.press('Escape');
		await expect(page.locator('.picker-panel')).toHaveCount(0);
	});

	test('a lane doorway measures its own agent, and Close returns to that lane with the scope kept', async ({ page, request }, testInfo) => {
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);
		await loginByUrl(page, { waitForComposer: false });

		// The Activity continue row is the reader's route from the rail to a lane.
		await openRail(page);
		await page.getByRole('button', { name: 'Activity', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await page.getByRole('button', { name: new RegExp(agentTitle) }).first().click();
		await expect(page.locator('.chat-col')).toBeVisible();
		await expect(page.locator('.shell-header')).toContainText(agentTitle);

		// PL-2: the doorway names itself and selects the lane it was opened from.
		const icon = page.getByRole('button', { name: `Measurement for ${agentTitle}`, exact: true });
		await expect(icon).toBeVisible();
		await expect(icon).toHaveText('');
		await expect(page.getByRole('button', { name: 'Measures', exact: true })).toHaveCount(0);
		const original = page.viewportSize();
		for (const width of [393, 1280]) {
			await page.setViewportSize({ width, height: 900 });
			await expect(icon).toBeVisible();
			const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
			expect(overflow, `agent-thread header has no horizontal overflow at ${width}`).toBeLessThanOrEqual(0);
			await page.screenshot({ path: testInfo.outputPath(`agent-header-${width}.png`) });
		}
		if (original) await page.setViewportSize(original);
		await icon.click();
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
		await expect(page.locator('.agent-chip')).toContainText(agentTitle);

		await page.getByRole('button', { name: 'Close Measurement', exact: true }).click();
		await expect(page.locator('.chat-col')).toBeVisible();
		await expect(page.locator('.shell-header')).toContainText(agentTitle);
		// Returning to the doorway must not lose what the reader was looking at.
		await expect(page.getByRole('button', { name: /^Measurement for/ })).toBeVisible();

		// The sidebar link opens the current project from the agent thread, never the agent.
		await openRail(page);
		await page.getByRole('link', { name: 'Measurement', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
		await expect(page.locator('.shell-header .h-title')).toHaveText(project.name);
		await expect(page.locator('.agent-chip')).toHaveCount(0);
		await page.screenshot({ path: testInfo.outputPath(`sidebar-click.png`) });
	});

	test('Project Activity opens the project scope, and a rail visit has no doorway to return to', async ({ page, request }, testInfo) => {
		// The doorway bookkeeping and the query-parameter contract are route logic, not
		// layout: they are verified on desktop here, while the narrow-viewport claims
		// (PL-5 popover containment, PL-6 one-line rows, the overflow doorway) run on
		// mobile in the picker and lane-doorway tests.
		test.skip((page.viewportSize()?.width ?? 0) <= 768, 'viewport-independent route behavior; the mobile surface is covered by the picker and doorway flows');
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);
		await loginByUrl(page, { waitForComposer: false });

		// PL-3: the project doorway selects the project, never an agent.
		await openRail(page);
		await page.getByRole('button', { name: /Jump to/ }).click();
		await page.locator('.palette .res').filter({ hasText: project.name }).first().click();
		await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await page.getByRole('button', { name: 'Measures', exact: true }).click();
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
		await expect(page.locator('.agent-chip')).toHaveCount(0);
		await page.getByRole('button', { name: 'Close Measurement', exact: true }).click();
		await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();

		// PL-2 fallback: entering from the rail invents no thread to go back to,
		// and a doorway opened after it still returns to its own lane.
		await openRail(page);
		await page.getByRole('link', { name: 'Measurement', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await page.getByRole('button', { name: /Measurement scope:/ }).click();
		await page.getByRole('option', { name: `All work` }).click();
		await expect(page.getByRole('button', { name: 'Measurement scope: All work', exact: true })).toBeVisible();

		await openRail(page);
		await page.getByRole('button', { name: 'Activity', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await page.getByRole('button', { name: new RegExp(agentTitle) }).first().click();
		await page.getByRole('button', { name: /^Measurement for/ }).click();
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
	});

	test('the page writes only the parameters it owns, and a rail visit has no doorway to return to', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 768, 'URL reading and writing is viewport-independent; verified on desktop');
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);

		await loginByUrl(page, { waitForComposer: false });
		await openRail(page);
		await page.getByRole('link', { name: 'Measurement', exact: true }).click();
		// The rail closed itself when its destination was chosen.
		railDismissed(page);
		await expect(page.getByRole('button', { name: 'Measurement scope: All work', exact: true })).toBeVisible();

		// Choosing the project in the scope select is the entry to the same state.
		await page.getByRole('button', { name: /Measurement scope:/ }).click();
		await page.getByRole('combobox', { name: 'Search projects and agents' }).fill(project.name);
		await page.getByRole('option', { name: new RegExp(project.name) }).first().click();
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
		await expect.poll(() => new URL(page.url()).searchParams.get('project')).toBe(project.id);

		await page.getByRole('group', { name: 'Grain' }).getByRole('button', { name: 'Weeks' }).click();
		await expect.poll(() => new URL(page.url()).searchParams.get('grain')).toBe('weeks');

		// A rail visit recorded no doorway, so Close falls back to Activity instead of
		// inventing a thread to go back to (PL-2).
		await page.getByRole('button', { name: 'Close Measurement', exact: true }).click();
		await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
	});
	test('a signed-in reader who pastes a measurement link lands on that scope, including an agent', async ({ page, request }, testInfo) => {
		// Desktop only: whether the scope survives the app taking the address over is
		// route behavior, not layout.
		test.skip((page.viewportSize()?.width ?? 0) <= 768, 'viewport-independent route behavior; verified on desktop');
		const suffix = `${testInfo.project.name}-${Date.now()}`;
		const project = await createProject(request, `Measurement P2 ${suffix}`);
		const agentTitle = `Measurement agent ${suffix}`;
		const agentId = await createAgent(request, agentTitle, project.id);
		await serveMeasurement(page, project, agentId, agentTitle);

		// Signed in first, so the arriving link carries no credential: this is the paste
		// case, not the disposable sign-in link.
		await loginByUrl(page, { waitForComposer: false });
		await page.goto(`/?view=measurement&project=${project.id}&agent=${agentId}&grain=weeks`);
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible({ timeout: 20_000 });
		await expect(page.locator('.agent-chip')).toContainText(agentTitle);
		await expect(page.getByRole('group', { name: 'Grain' }).getByRole('button', { name: 'Weeks' })).toHaveAttribute('aria-pressed', 'true');
	});

});
