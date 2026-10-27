/**
 * The Agents table against the producer it is served by, in a real browser.
 *
 * `measurement-agents-populated.spec.ts` intercepts the Agents route and fulfils its own
 * fixture, so it proves the decoder, the store and the table agree with each other — it
 * cannot show that the producer's bytes reach the table, and it never renders a failed
 * load. These two scenarios close both halves over the network boundary:
 *
 * 1. a `200` that cannot support the claims the surface makes must render the existing
 *    failed-load alert and Retry, must not fall back to the empty-scope headline or an
 *    order claim, and Retry must recover to the REAL served answer;
 * 2. the seeded population must arrive through the route un-stubbed, with a cell for every
 *    column the table displays.
 *
 * Interception here is only ever a fault injector for scenario 1's first response. The
 * population is seeded into the wrapper's own disposable state database through the
 * supported helper pattern already used by `focus-mode-transcript.spec.ts`, so no backend
 * test endpoint and no wrapper lifecycle change is involved.
 */

import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { loginByUrl, openSettings } from '../support/corsia';
import { selectProjectScope } from '../support/measurement-fixture';
// A relative path, not the `$lib` alias: Playwright loads specs with its own TS transform,
// which does not read the alias SvelteKit writes to .svelte-kit/tsconfig.json. The leaf is
// type-only at its one import, so nothing pulls a store into the node process.
import { agentsColumns } from '../../src/lib/api/measurementAgentsTypes';

/** The producer serves one cell per `TABLE_METRICS` entry; the table displays a subset of them. */
// The consumer's own displayed columns, imported instead of restated: the guard asks the
// same question the table answers, so a column added to the table is validated against the
// served bytes without this list following it.
const DISPLAYED_METRICS = agentsColumns.map((column) => column.id);

function seedAgentsPopulation(): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!stateRoot || !python || path.basename(stateRoot) !== 'e2e-state') {
		throw new Error('Agents served proof requires the disposable E2E Python and state root');
	}
	// One completed day, three accepted task nodes, exactly the shape the route's own
	// query path reads: an enabled agent thread in a project, its plan, the settled nodes
	// and the events that settled them. The page defaults to Days, so the seed belongs in
	// yesterday's completed period rather than an older weekly bucket.
	execFileSync(python, ['-c', [
		'import datetime, sqlite3, sys',
		'connection = sqlite3.connect(sys.argv[1])',
		'connection.row_factory = sqlite3.Row',
		'user = connection.execute("SELECT id FROM users ORDER BY created_at LIMIT 1").fetchone()',
		'assert user is not None, "the disposable install has no user to own the agent"',
		'today = datetime.date.today()',
		'completed_day = today - datetime.timedelta(days=1)',
		'settled = f"{completed_day.isoformat()}T12:00:00Z"',
		'agent, project = "e2e-measured-agent", "e2e-measured-project"',
		'connection.execute("INSERT OR IGNORE INTO projects (id,name,created_at,updated_at) VALUES (?,?,?,?)", (project, project, settled, settled))',
		'cols = "id,user_id,title,project_id,parent_thread_id,agent_enabled,agent_status,agent_responsibility,created_at,updated_at"',
		'connection.execute(f"INSERT OR IGNORE INTO threads ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)", (agent, user[0], agent, project, None, 1, "active", "Owns measurement", settled, settled))',
		'connection.execute("INSERT OR IGNORE INTO plans (id,owner_kind,owner_id,state,revision,created_at,updated_at) VALUES (?,\'thread\',?,\'active\',1,?,?)", (f"plan-{agent}", agent, settled, settled))',
		'nodes = "id,plan_id,parent_id,order_key,kind,title,acceptance_criteria,state,commitment_id,requirements_revision,revision,difficulty,created_at,updated_at"',
		'events = "id,node_id,provenance,event,prior_state,new_state,created_at"',
		'for index in range(3):',
		'    node = f"{agent}-node-{index}"',
		'    connection.execute(f"INSERT OR IGNORE INTO plan_nodes ({nodes}) VALUES (?,?,NULL,?,?,?,?,?,?,1,1,?,?,?)", (node, f"plan-{agent}", index, "task", "Measured task", "done", "accepted", f"commitment-{agent}", "standard", settled, settled))',
		'    connection.execute(f"INSERT OR IGNORE INTO plan_node_events ({events}) VALUES (?,?,\'e2e\',\'accepted\',\'active\',\'accepted\',?)", (f"event-{node}", node, settled))',
		'from plans.public import rebuild_plan_aggregates',
		'rebuild_plan_aggregates(connection, f"plan-{agent}")',
		'connection.commit()',
		'print(f"seeded agent={agent} project={project} user={user[0]} day={completed_day}")',
	].join('\n'), `${stateRoot}/corsia.db`], {
		stdio: ['ignore', 'inherit', 'inherit'],
		// The rebuild below imports the backend, so run it the way the other e2e seeders do.
		cwd: path.resolve('..'),
		env: {
			...process.env,
			PYTHONPATH: 'backend',
			CORSIA_TEST_MODE: '1',
			CORSIA_ANALYTICS_DISABLE: '1',
			CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE,
			CORSIA_STATE_DIR: stateRoot,
		},
	});
}

/** Stub only the trend panels so this surface is the Agents table on its own. */
async function isolateAgentsPanel(page: import('@playwright/test').Page): Promise<void> {
	await page.route('**/api/measurement/series/index?**', async (route) => {
		await route.fulfill({ json: { definitions: [] } });
	});
	await page.route('**/api/measurement/series?**', async (route) => {
		await route.fulfill({ json: { rows: [] } });
	});
}

const SEEDED_PROJECT = 'e2e-measured-project';

async function openMeasurement(page: import('@playwright/test').Page) {
	await loginByUrl(page, { waitForComposer: false });
	await openSettings(page);
	// Below 768px the rail is a drawer that closed itself when Settings opened.
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	// Two different regions, and the distinction is the whole point of scenario 1: the panel
	// (the "Biggest changes" card, which holds the Agents section) is what renders the
	// failed-load alert, while the "Agents table" region is the scroll box that only exists
	// once rows are on screen. Scoping a failure assertion to the table region would prove
	// nothing, because a failed load never builds one. Both exist only at a project's scope.
	return {
		panel: page.getByRole('region', { name: 'Biggest changes' }),
		table: page.getByRole('region', { name: /Agents table/ }),
	};
}

/** Pick the seeded project from the page's Showing select, which is how a reader reaches its Agents table. */
async function openSeededProject(page: import('@playwright/test').Page) {
	await selectProjectScope(page, SEEDED_PROJECT);
	await expect(page.getByRole('button', { name: 'Showing: ' + SEEDED_PROJECT, exact: true })).toBeVisible();
}

test.describe('@surface:measurement agents served by the producer', () => {
	test('a 200 that cannot support the claims renders the alert and Retry, then recovers to the real answer', async ({ page }) => {
		seedAgentsPopulation();
		await isolateAgentsPanel(page);

		// The served keys the decoder consumes, minus `project_summaries`,
		// `inactive_projects` and `definition_version`: an answer that HTTPs `200` while
		// leaving the project lists and version out, and that omits an unselected metric
		// cell, is exactly what used to render as a finished measurement.
		const unsupportable = {
			rows: [{
				agent: { id: 'e2e-measured-agent', name: 'e2e-measured-agent', project: { id: 'e2e-measured-project', name: 'e2e-measured-project' } },
				rank: 1, status: 'eligible', selected_metric: 'fresh_tokens_per_outcome', n: 3,
				difficulty_mix: { standard: 3 },
				metrics: { outcomes: { value: 3, n: 3, minimum_n: 20, periods: [] }, fresh_tokens_per_outcome: { value: null, n: 3, minimum_n: 20, periods: [] } },
				worsening: { eligible: false, reason: 'too_little_history', metric: 'fresh_tokens_per_outcome', minimum_n: 20 },
				flags: {},
			}],
			findings: [], findings_state: 'healthy', caption: 'Trailing 8 completed weeks',
			period: { grain: 'days' }, sort: 'ranked', metric: 'fresh_tokens_per_outcome',
			scope: { project: SEEDED_PROJECT, agent: null },
		};
		// The fault is injected into the first answer for the project's scope only; All work
		// is served for real, so the project list the reader picks from is the producer's.
		let injected = false;
		await page.route('**/api/measurement/agents?**', async (route) => {
			if (injected || new URL(route.request().url()).searchParams.get('project') !== SEEDED_PROJECT) return route.fallback();
			injected = true;
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(unsupportable) });
		});

		const { panel, table } = await openMeasurement(page);
		await openSeededProject(page);
		// The panel can report the same failed load on more than one surface, so the claims are
		// read from the first match and counted separately, never assumed unique.
		await expect(panel.getByRole('alert').filter({ hasText: 'Could not load the agents table.' }).first()).toBeVisible();
		await expect(panel.getByRole('button', { name: 'Retry' }).first()).toBeVisible();
		// The two claims a defaulted load would have made: an empty population and an
		// ordering of rows that are not there.
		await expect(page.getByText('No active agents for this selection.')).toHaveCount(0);
		await expect(table).toHaveCount(0);
		await expect(panel.locator('[aria-sort]')).toHaveCount(0);

		// Drop the fault injector only; the next request goes to the real backend, which
		// answers for the seeded agent.
		await page.unroute('**/api/measurement/agents?**');
		const served = page.waitForResponse((response) => response.url().includes('/api/measurement/agents') && response.url().includes('project=' + SEEDED_PROJECT));
		await panel.getByRole('button', { name: 'Retry' }).first().click();
		const body = await (await served).json();
		await expect(panel.getByRole('alert')).toHaveCount(0);
		expect(body.rows.length, 'the seeded agent must be served for real').toBeGreaterThan(0);
		await expect(table.locator('tbody tr')).toHaveCount(body.rows.length);

		// Producer-to-table shape, which a fixture-only scenario cannot establish: every
		// displayed column has a cell in the bytes the route actually served.
		for (const row of body.rows) {
			for (const metric of DISPLAYED_METRICS) {
				expect(row.metrics, `row for ${row.agent.name} omits the displayed column ${metric}`).toHaveProperty(metric);
			}
		}
		console.log(`served after Retry: ${body.rows.length} rows, findings_state=${body.findings_state}, grain=${body.period?.grain}`);
	});

	test('the real served population renders with a cell for every displayed column', async ({ page }) => {
		seedAgentsPopulation();
		// No Agents interception at all: this scenario is the network boundary itself.
		await isolateAgentsPanel(page);
		const { panel, table } = await openMeasurement(page);
		// All work is served for real too; the Agents table belongs to a project's scope.
		await expect(table).toHaveCount(0);
		const served = page.waitForResponse((response) => response.url().includes('/api/measurement/agents') && response.url().includes('project=' + SEEDED_PROJECT));
		await openSeededProject(page);
		const body = await (await served).json();
		expect(body.rows.length, 'the seeded agent must reach the table un-stubbed').toBeGreaterThan(0);
		await expect(panel.getByRole('alert')).toHaveCount(0);
		await expect(table.locator('tbody tr')).toHaveCount(body.rows.length);
		// One cell per displayed column, plus the outcomes count and the cost per hour the markup
		// puts around them (the agent name is the row header, not a td); the length comes from the consumer's
		// own column list, so a column cannot be added to the table without this number
		// following it.
		const cells = table.locator('tbody tr').first().locator('td');
		await expect(cells).toHaveCount(DISPLAYED_METRICS.length + 2);
		// The producer serves more metrics than the table shows (the rest feed sorting and findings),
		// so the served keys must contain every displayed column, not equal them.
		expect(Object.keys(body.rows[0].metrics)).toEqual(expect.arrayContaining(DISPLAYED_METRICS));
		await expect(table.locator('[aria-sort]').first()).toBeVisible();
		console.log(`real served row: ${JSON.stringify(Object.keys(body.rows[0].metrics))} n=${body.rows[0].n} status=${body.rows[0].status}`);
	});
});
