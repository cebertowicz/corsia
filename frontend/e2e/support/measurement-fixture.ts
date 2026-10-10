import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { e2eModel, TEST_TOKEN } from './corsia';

const tok = encodeURIComponent(TEST_TOKEN);

// Shared by the flows that need a populated Measurement scope on a disposable instance.
export async function createProject(request: APIRequestContext, name: string) {
	const response = await request.post(`/api/projects?token=${tok}`, {
		data: { name, description: 'Synthetic Models width fixture', status: 'active', accent_color: '#d65a87' },
	});
	expect(response.ok()).toBeTruthy();
	const projects = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
	const project = (projects.projects || []).find((candidate: { name: string }) => candidate.name === name);
	expect(project, 'the seeded project is available to the scope label').toBeTruthy();
	return project as { id: string; name: string; path: string };
}

export async function createAgent(request: APIRequestContext, title: string, projectId: string) {
	const created = await request.post(`/api/threads?token=${tok}`, {
		data: { title, project_id: projectId, model: e2eModel() },
	});
	expect(created.ok()).toBeTruthy();
	const { thread_id: threadId } = await created.json() as { thread_id: string };
	const promoted = await request.patch(`/api/agents/${threadId}?token=${tok}`, {
		data: { responsibility: `Synthetic Models width lane for ${title}`, active: true },
	});
	expect(promoted.ok()).toBeTruthy();
	return threadId;
}

export const column = (value: number | null) => ({ value, n: 20, minimum_n: 20, periods: [] });
export const rule = (flagged: boolean) => ({
	eligible: true, metric: 'fresh_tokens_per_outcome', minimum_n: 20, flagged,
	current: 140, mean: 100, sd: 12, standardized_size: 3.3, zero_variance_baseline: false,
	relative_adverse_change: 0.4, action: 'Review its costliest recent tasks.', window: 'trailing 8 weeks',
});

/** The Agents envelope the surface reads its scope label from. */
export function agentsBody(project: { id: string; name: string }, agentId: string, agentName: string, request: URL) {
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


// Synthetic size coverage: the latest week is partially rated, with a rate and
// visible unrated, unknown and stale counts, so the size headline renders every line.
export const sizeCoverage = {
		outcomes: 762, sized: 520, estimated_hours: 1292.5, active_hours: 40.4, unsized: 230, unknown: 8, stale: 4, conflict: 0,
		retracted: 0, malformed: 0, basis: { delivered_evidence_proxy: 410, accepted_scope_proxy: 110, author_declared: 0 },
		profiles: [{ method: 'offline_proxy', method_version: '1', rubric_version: '1.3p', rater: 'synthetic-rater', rater_version: 'unknown', outcomes: 520 }],
		comparable_reason: 'outcome_definition_changed', rate_reason: null, class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 },
		data_start: '2026-08-31', comparability_before: '2026-08-31',
	};

// The key-figures block the key-figures endpoint returns, for specs that stub that route:
// a huge token total with uncounted calls, so the row's widest figures are on the page. The four lines add up to the total.
const kpiTokens = (total: number, unknown_records = 0) => ({
	total: { value: total, unknown_records },
	input: { value: total - Math.round(total / 20) - Math.round(total / 2) - Math.round(total / 40), unknown_records }, cache_write: { value: Math.round(total / 20), unknown_records },
	cache_read: { value: Math.round(total / 2), unknown_records: 0 }, output: { value: Math.round(total / 40), unknown_records },
});
export const kpiBody = {
	current: { start: '2026-10-08T07:00:00.000000', end: '2026-10-09T07:00:00.000000', from: '2026-09-28', to: '2026-10-04', tokens: kpiTokens(123_456_789_012, 49) },
	previous: { start: '2026-10-07T07:00:00.000000', end: '2026-10-08T07:00:00.000000', from: '2026-09-21', to: '2026-09-27', tokens: kpiTokens(655_800_000) },
};

/** Open a project's Measurement scope from the page's Scope select, the one way to leave All work. */
export async function selectProjectScope(page: Page, projectName: string): Promise<void> {
	await page.getByRole('button', { name: /Measurement scope:/ }).click();
	await page.getByRole('combobox', { name: 'Search projects and agents' }).fill(projectName);
	await page.getByRole('option', { name: new RegExp(`^${projectName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) }).first().click();
}

/** The echoed period every grain-aware Measurement route serves: the last completed week (or day) ends the window, and the response names it. */
export function mockPeriod(grain: string | null) {
	const shift = (iso: string, days: number) => new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86_400_000).toISOString().slice(0, 10);
	const days = grain !== 'weeks';
	const last = days ? '2026-10-04' : '2026-09-28';
	const latestFrom = shift(last, days ? -6 : -21);
	const latestTo = days ? last : shift(last, 6);
	const length = days ? 7 : 28;
	return {
		grain: days ? 'days' : 'weeks', timezone: 'America/Los_Angeles', current: shift(latestTo, 1),
		range: { from: shift(latestTo, days ? -27 : -83), to: latestTo },
		latest: { from: latestFrom, to: latestTo }, previous: { from: shift(latestFrom, -length), to: shift(latestFrom, -1) },
	};
}

/** How many periods the moving average pools: 7 days, or 4 weeks, as the series answer's `average_periods` says. */
export const mockAveragePeriods = (grain: string | null) => (grain === 'weeks' ? 4 : 7);

/** The windows block a series answer serves: each id's pooled latest and previous figure with the n behind it. */
export function mockWindows(ids: string[]) {
	const rate = (id: string) => id === 'first_try_acceptance';
	return Object.fromEntries(ids.map((id) => [id, {
		latest: { value: rate(id) ? 0.82 : 12.5, n: 120 }, previous: { value: rate(id) ? 0.8 : 12, n: 110 },
	}]));
}

/** A populated Cost answer for the echoed period, for flows that are not about Cost. */
export function mockCostBody(grain: string | null) {
	const period = (day: string, dollars: number, outcomes: number) => ({
		period: day, dollars, priced_records: 800, api_records: 1_000, outcomes, per_outcome: dollars / outcomes, sized: 120, hours: 2_400, per_hour: dollars / 2_400, average: dollars / 2_400,
	});
	// The served latest and previous windows are named by the days they cover, not by a `period` key.
	const window = (row: ReturnType<typeof period>, from: string, to: string) => { const { period: _period, ...figures } = row; return { from, to, ...figures }; };
	const previous = period('2026-09-21', 5_300, 1_425);
	const latest = period('2026-09-28', 10_600, 3_746);
	return {
		definition_version: 1, period: mockPeriod(grain), series: [previous, latest], latest: window(latest, '2026-09-28', '2026-10-04'), previous: window(previous, '2026-09-21', '2026-09-27'),
		coverage: { priced_records: 1_600, api_records: 2_000 }, not_api_priced_records: 0, excluded: [],
		actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: { subscription_dollars: null, overage_dollars: null, subscription_facts: 0, overage_facts: 0, subscription_coverage: 'none', overage_coverage: 'none', overlapping_overage: [], allocation: 'unavailable' } },
	};
}
