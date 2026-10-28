import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings } from '../support/corsia';
import { createProject, selectProjectScope } from '../support/measurement-fixture';

/**
 * The Agents table with rows in it, on the real composed surface.
 *
 * The headline spec proves the empty page; it never renders a populated row, so it
 * cannot see what the approved ranking safeguards look like once there is something
 * to rank: the order control has to say that the agent furthest behind leads, each
 * row has to show the task mix behind its count, the confound caption has to be
 * visible, and a wide table at 393px has to stay reachable inside one scroll region
 * instead of pushing the page sideways.
 */

const column = (value: number | null) => ({ value, n: 200, minimum_n: 20, periods: [] });
const rule = (flagged: boolean) => ({
	eligible: true, metric: 'fresh_tokens_per_outcome', minimum_n: 20, flagged,
	current: 140, mean: 100, sd: 12, standardized_size: 3.3, zero_variance_baseline: false,
	relative_adverse_change: 0.4, action: 'Review its 5 costliest recent tasks.', window: 'trailing 8 weeks',
});

/**
 * Served as the producer does for each scope: All work is the project list (no rows, no
 * findings of its own), a project is the Agents table. Rows are in rank order, furthest
 * behind first, so the costliest row leads the column.
 */
function agentsResponse(scope: { project: string; agent: string | null }, project: { id: string; name: string }) {
	const allWork = scope.project === 'all' && scope.agent === null;
	return {
		definition_version: 1,
		scope,
		sort: 'ranked',
		metric: 'fresh_tokens_per_outcome',
		period: { grain: 'days' },
		caption: 'Trailing 8 completed weeks',
		findings_state: allWork ? 'healthy' : 'flagged',
		findings: allWork ? [] : [
			{
				agent: { id: 'agent-behind', name: 'Behind', project: { id: project.id, name: project.name } },
				metric: 'fresh_tokens_per_outcome', current: 140, mean: 100, sd: 12, n: 160,
				standardized_size: 3.33, zero_variance_baseline: false, relative_adverse_change: 0.4,
				minimum_n: 20, window: { from: '2026-08-03', to: '2026-09-21' },
				action: 'Review its 5 costliest recent tasks.',
			},
		],
		project_summaries: allWork ? [{ project: { id: project.id, name: project.name }, flagged_agents: 1, outcomes: 60 }] : [],
		inactive_projects: [],
		rows: allWork ? [] : [
			{
				agent: { id: 'agent-behind', name: 'Behind', project: { id: project.id, name: project.name } },
				n: 160, difficulty_mix: { hard: 90, routine: 10, standard: 60 }, status: 'eligible', rank: 3,
				selected_metric: 'fresh_tokens_per_outcome',
				metrics: {
					outcomes: column(20), fresh_tokens_per_outcome: column(140),
					first_try_acceptance: column(0.7), asks_per_outcome: column(0.1),
					active_hours_per_outcome: { ...column(0.05), previous_value: 0.1, previous_n: 20 },
				},
				worsening: rule(true), flags: { fresh_tokens_per_outcome: rule(true), first_try_acceptance: rule(false) },
			},
			{
				agent: { id: 'agent-middle', name: 'Middle', project: { id: project.id, name: project.name } },
				n: 160, difficulty_mix: { standard: 160 }, status: 'eligible', rank: 2,
				selected_metric: 'fresh_tokens_per_outcome',
				metrics: {
					outcomes: column(20), fresh_tokens_per_outcome: column(110),
					first_try_acceptance: column(0.8), asks_per_outcome: column(0.1),
					active_hours_per_outcome: { ...column(0.1), previous_value: 0.1, previous_n: 20 },
				},
				worsening: rule(false), flags: { fresh_tokens_per_outcome: rule(false), first_try_acceptance: rule(false) },
			},
			{
				agent: { id: 'agent-ahead', name: 'Ahead', project: { id: null, name: null } },
				n: 160, difficulty_mix: {}, status: 'eligible', rank: 1,
				selected_metric: 'fresh_tokens_per_outcome',
				metrics: {
					outcomes: column(20), fresh_tokens_per_outcome: column(90),
					first_try_acceptance: column(0.9), asks_per_outcome: column(0.2),
					active_hours_per_outcome: { ...column(0.2), previous_value: 0.1, previous_n: 20 },
				},
				worsening: rule(false), flags: { fresh_tokens_per_outcome: rule(false), first_try_acceptance: rule(false) },
			},
		],
	};
}

async function openMeasurement(page: import('@playwright/test').Page, project: { id: string; name: string }): Promise<void> {
	await page.route('**/api/measurement/series/index?**', async (route) => {
		await route.fulfill({ json: { definitions: [] } });
	});
	await page.route('**/api/measurement/series?**', async (route) => {
		await route.fulfill({ json: { rows: [] } });
	});
	await page.route('**/api/measurement/agents?**', async (route) => {
		const params = new URL(route.request().url()).searchParams;
		const agent = params.get('agent');
		await route.fulfill({ json: agentsResponse({ project: params.get('project') ?? 'all', agent: agent === 'none' ? null : agent }, project) });
	});
	await loginByUrl(page, { waitForComposer: false });
	await openSettings(page);
	// Below 768px the rail is a drawer that closed itself when Settings opened.
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	// The Agents table belongs to a project's scope, reached from the page's Showing select.
	await expect(page.getByRole('region', { name: /Agents table/ })).toHaveCount(0);
	await selectProjectScope(page, project.name);
	await expect(page.getByRole('button', { name: `Showing: ${project.name}`, exact: true })).toBeVisible();
	const table = page.getByRole('region', { name: /Agents table/ });
	await expect(table.locator('tbody tr')).toHaveCount(3);
}

/** Back to All work through the scope picker: the table gives way. */
async function backToAllWork(page: import('@playwright/test').Page): Promise<void> {
	await page.getByRole('button', { name: /Showing:/ }).click();
	await page.getByRole('option', { name: 'All work' }).click();
	await expect(page.getByRole('button', { name: 'Showing: All work', exact: true })).toBeVisible();
	await expect(page.getByRole('region', { name: /Agents table/ })).toHaveCount(0);
}

test.describe('@surface:measurement agents populated', () => {
	test('shows the ranked order, the task mix and a reachable wide table', async ({ page, request }, testInfo) => {
		const project = await createProject(request, `Agents populated ${Date.now()}`);
		for (const width of [1440, 393] as const) {
			await page.setViewportSize({ width, height: 1000 });
			await openMeasurement(page, project);
			const table = page.getByRole('region', { name: /Agents table/ });

			// The table opens with the most outcomes first, with no column sorted.
			const names = table.locator('tbody tr th .name');
			await expect(names).toHaveText(['Ahead', 'Behind', 'Middle']);
			await expect(table.locator('thead th')).toHaveText([
				'Agent', 'Outcomes', 'First try', '$ per person-hour of work delivered', 'Change in minutes per outcome, vs the previous window',
			]);
			await expect(table.locator('thead th').nth(1)).toHaveAttribute('aria-sort', 'none');

			// A header click sorts that column ascending, then descending (First try rises toward Ahead).
			const firstTry = table.locator('thead th').nth(2);
			await expect(firstTry).toHaveAttribute('aria-sort', 'none');
			await firstTry.getByRole('button').click();
			await expect(firstTry).toHaveAttribute('aria-sort', 'ascending');
			await expect(names).toHaveText(['Behind', 'Middle', 'Ahead']);
			await firstTry.getByRole('button').click();
			await expect(firstTry).toHaveAttribute('aria-sort', 'descending');
			await expect(names).toHaveText(['Ahead', 'Middle', 'Behind']);
			await expect(table.locator('thead th').nth(1)).toHaveAttribute('aria-sort', 'none');

			// The page never scrolls sideways itself; the table's own region takes the overflow.
			const overflow = await page.evaluate(() => ({
				page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
				region: (() => {
					const region = document.querySelector<HTMLElement>('.table-wrap');
					return region ? region.scrollWidth - region.clientWidth : -1;
				})(),
			}));
			expect(overflow.page).toBeLessThanOrEqual(0);
			if (width === 393) {
				// The narrow screen is where the table is tightest: all five columns fit the region, so
				// nothing needs a sideways scroll to be read; every agent's filter action and the
				// last column stay reachable inside it, never past the page edge.
				expect(overflow.region).toBeLessThanOrEqual(0);
				const reached = await page.evaluate(() => {
					const region = document.querySelector<HTMLElement>('.table-wrap');
					if (!region) return null;
					const box = region.getBoundingClientRect();
					const inside = (element: Element) => {
						const cell = element.getBoundingClientRect();
						return cell.right <= box.right + 1 && cell.left >= box.left - 1;
					};
					const actions = Array.from(region.querySelectorAll<HTMLElement>('button.filter'));
					const actionsReachable = actions.every(inside);
					region.scrollLeft = region.scrollWidth;
					const headers = region.querySelectorAll('thead th');
					return { actions: actions.length, actionsReachable, lastColumnReachable: inside(headers[headers.length - 1]) };
				});
				expect(reached?.actions).toBe(3);
				expect(reached?.actionsReachable).toBe(true);
				expect(reached?.lastColumnReachable).toBe(true);
			}
			await page.screenshot({ path: testInfo.outputPath(`measurement-agents-${width}.png`), fullPage: true, animations: 'disabled' });
			await backToAllWork(page);
		}
	});
});
