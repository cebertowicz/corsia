import { expect, test, type APIRequestContext, type Locator, type Page, type TestInfo } from '@playwright/test';
import { loginByUrl } from '../support/corsia';
import { agentsBody, createAgent, createProject } from '../support/measurement-fixture';

/**
 * The key-figures row at the top of Measurement: five tiles over the grain's latest window.
 *
 * The claim proved here is what a person sees: Work per hour, First-try rate, Cost per hour, Your
 * minutes per outcome and Tokens (the total with its uncached input, cache write, cache read and
 * output beneath), each a number and a short unit with the window, its n and the change against the
 * window before; five in one row at desktop width, stacked at phone width with nothing scrolling
 * sideways; the same type scale on every tile; and the grain control moving every tile. Measurement
 * is served from this file, as the other Measurement flows do.
 */

const figure = (value: number, unknown_records = 0) => ({ value, unknown_records });
const window = (value: number | null, n: number) => ({ value, n });
const span = (latest: [string, string], previous: [string, string]) => ({
	latest: { from: latest[0], to: latest[1] }, previous: { from: previous[0], to: previous[1] },
});
const tokens = (total: number, input: number, write: number, read: number, output: number) => ({
	total: figure(total), input: figure(input), cache_write: figure(write), cache_read: figure(read), output: figure(output),
});

interface Scenario {
	period: ReturnType<typeof span>;
	size: ReturnType<typeof window>[]; firstTry: ReturnType<typeof window>[]; minutes: ReturnType<typeof window>[];
	cost: { value: number | null; n: number }[];
	kpi: { current: Record<string, unknown>; previous: Record<string, unknown> };
	expected: Record<string, string>;
}
const days: Scenario = {
	period: span(['2026-09-28', '2026-10-04'], ['2026-09-21', '2026-09-27']),
	size: [window(92.4, 1349), window(30.2, 451)], firstTry: [window(0.89, 1377), window(0.85, 451)], minutes: [window(0.03, 1377), window(0.097, 451)],
	cost: [{ value: 1.49, n: 1187 }, { value: 1.55, n: 399 }],
	kpi: {
		current: { start: 's', end: 'e', from: '2026-09-28', to: '2026-10-04', tokens: tokens(34_590_000_000, 526_000_000, 278_000_000, 33_660_000_000, 119_000_000) },
		previous: { start: 's', end: 'e', from: '2026-09-21', to: '2026-09-27', tokens: tokens(15_300_000_000, 1, 1, 1, 1) },
	},
	expected: {
		work: 'Work per hour of your attention 92 h Sep 28 to Oct 4, n 1,349, +206% vs Sep 21 to Sep 27 (n 451)',
		'first-try': 'First-try rate (agent-graded) 89% Sep 28 to Oct 4, n 1,377, +5% vs Sep 21 to Sep 27 (n 451)',
		cost: 'Cost per person-hour of work delivered $1.49 Sep 28 to Oct 4, n 1,187, -4% vs Sep 21 to Sep 27 (n 399)',
		minutes: 'Your minutes per outcome 1.8 min Sep 28 to Oct 4, n 1,377, -69% vs Sep 21 to Sep 27 (n 451)',
		tokens: 'Tokens 34.6B Sep 28 to Oct 4, +126% vs Sep 21 to Sep 27 Uncached input 526M Cache write 278M Cache read 33.7B Output 119M',
	},
};
const weeksScenario: Scenario = {
	period: span(['2026-09-07', '2026-10-04'], ['2026-08-10', '2026-09-06']),
	size: [window(53.2, 2317), window(null, 450)], firstTry: [window(0.89, 2345), window(null, 450)], minutes: [window(0.0617, 2345), window(null, 450)],
	cost: [{ value: 1.51, n: 1742 }, { value: null, n: 0 }],
	kpi: {
		current: { start: 's', end: 'e', from: '2026-09-07', to: '2026-10-04', tokens: tokens(58_050_000_000, 838_000_000, 549_000_000, 56_460_000_000, 204_000_000) },
		previous: { start: 's', end: 'e', from: '2026-08-10', to: '2026-09-06', tokens: { ...tokens(0, 0, 0, 0, 0) } },
	},
	expected: {
		work: 'Work per hour of your attention 53 h Sep 7 to Oct 4, n 2,317, new vs Aug 10 to Sep 6 (n 450)',
		'first-try': 'First-try rate (agent-graded) 89% Sep 7 to Oct 4, n 2,345, new vs Aug 10 to Sep 6 (n 450)',
		cost: 'Cost per person-hour of work delivered $1.51 Sep 7 to Oct 4, n 1,742, new vs Aug 10 to Sep 6 (n 0)',
		minutes: 'Your minutes per outcome 3.7 min Sep 7 to Oct 4, n 2,345, new vs Aug 10 to Sep 6 (n 450)',
		tokens: 'Tokens 58B Sep 7 to Oct 4, new vs Aug 10 to Sep 6 Uncached input 838M Cache write 549M Cache read 56.5B Output 204M',
	},
};

const costPeriod = (figure: { value: number | null; n: number }, period: string, n = figure.n) => ({ period, dollars: figure.value === null ? null : figure.value * 1_000, priced_records: 1, usage_records: 1, outcomes: n, per_outcome: figure.value === null ? null : 1, sized: n, hours: 1_000, per_hour: figure.value });
/** A pooled comparison window as the Cost payload serves it: named by the days it covers, with no `period` key. */
const pooled = ({ period: _period, ...figures }: ReturnType<typeof costPeriod>, bounds: { from: string; to: string }) => ({ from: bounds.from, to: bounds.to, ...figures });

const stepDays = (grain: string) => (grain === 'days' ? 1 : 7);
const addDays = (iso: string, days: number) => new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86_400_000).toISOString().slice(0, 10);
const sizeCoverage = (estimated_hours: number, sized: number, data_start: string) => ({
	outcomes: sized, sized, estimated_hours, active_hours: 1, unsized: 0, unknown: 0, stale: 0, conflict: 0, retracted: 0, malformed: 0,
	basis: { delivered_evidence_proxy: sized, accepted_scope_proxy: 0, author_declared: 0 }, profiles: [],
	comparable_reason: null, rate_reason: null, class_midpoint_hours: { XS: 0.1, S: 0.5, M: 2, L: 8, XL: 40, XXL: 120 }, data_start, comparability_before: data_start,
});

/** The rows behind a windowed measure: one per period of the grain across both served windows, the window's n split over its periods, so the section headline and its tile read the same rows. */
function costRows(period: { latest: { from: string; to: string }; previous: { from: string; to: string } }, grain: string, served: { value: number | null; n: number }[]) {
	const step = stepDays(grain);
	const dates: string[] = [];
	for (let date = period.previous.from; date <= period.latest.to; date = addDays(date, step)) dates.push(date);
	return dates.map((date) => {
		const inPrevious = date <= period.previous.to;
		const figure = served[inPrevious ? 1 : 0];
		const bounds = inPrevious ? period.previous : period.latest;
		const count = dates.filter((d) => (d <= period.previous.to) === inPrevious).length;
		const n = Math.floor(figure.n / count) + (date === bounds.from ? figure.n % count : 0);
		return costPeriod(figure, date, n);
	});
}

function rowsFor(series: string, grain: string, period: { latest: { from: string; to: string }; previous: { from: string; to: string } }, served?: ReturnType<typeof window>[]) {
	const step = stepDays(grain);
	const dates: string[] = [];
	for (let date = period.previous.from; date <= period.latest.to; date = addDays(date, step)) dates.push(date);
	return dates.map((date) => {
		const inPrevious = date <= period.previous.to;
		const figure = served?.[inPrevious ? 1 : 0];
		const bounds = inPrevious ? period.previous : period.latest;
		const count = dates.filter((d) => (d <= period.previous.to) === inPrevious).length;
		const n = figure ? Math.floor(figure.n / count) + (date === bounds.from ? figure.n % count : 0) : 20;
		const value = series === 'outcomes' ? 20 : figure ? figure.value : 1;
		return {
			period: date, series, value, n, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false,
			...(series === 'estimated_human_hours_per_active_hour' ? { size: sizeCoverage(value ?? 0, value === null ? 0 : n, '2026-09-01T00:00:00Z') } : {}),
		};
	});
}

async function serve(page: Page, project: { id: string; name: string }, agentId: string, agentName: string) {
	await page.route('**/api/measurement/**', async (route) => {
		const url = new URL(route.request().url());
		const scenario = url.searchParams.get('grain') === 'weeks' ? weeksScenario : days;
		const grain = url.searchParams.get('grain') === 'weeks' ? 'weeks' : 'days';
		const period = { grain, timezone: 'America/Los_Angeles', current: scenario.period.latest.to, range: { from: scenario.period.previous.from, to: scenario.period.latest.to }, ...scenario.period };
		if (url.pathname.endsWith('/key-figures')) return route.fulfill({ json: { definition_version: 1, scope: { project: url.searchParams.get('project'), agent: null }, today: '2026-10-05', period, kpi: scenario.kpi } });
		if (url.pathname.endsWith('/agents')) return route.fulfill({ json: agentsBody(project, agentId, agentName, url) });
		if (url.pathname.endsWith('/models')) return route.fulfill({ json: { rows: [], min_tasks: 14, token_shares: [], unmatched_failed_turns: 0, unresolved_token_share: 0, total_spend_usd: 0 } });
		if (url.pathname.endsWith('/cost')) {
			const [latestFigure, previousFigure] = scenario.cost;
			const latest = costPeriod(latestFigure, scenario.period.latest.from);
			const previous = costPeriod(previousFigure, scenario.period.previous.from);
			return route.fulfill({ json: { definition_version: 1, period, series: costRows(scenario.period, grain, scenario.cost), latest: pooled(latest, scenario.period.latest), previous: pooled(previous, scenario.period.previous), coverage: { priced_records: 3, usage_records: 3 }, not_api_priced_records: 0, excluded: [], actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: null } } });
		}
		const ids = url.searchParams.get('ids')?.split(',').filter(Boolean) ?? [];
		const served: Record<string, ReturnType<typeof window>[]> = { estimated_human_hours_per_active_hour: scenario.size, first_try_acceptance: scenario.firstTry, active_hours_per_outcome: scenario.minutes };
		return route.fulfill({ json: {
			period,
			windows: Object.fromEntries(ids.map((id) => [id, { latest: (served[id] ?? [window(1, 20)])[0], previous: (served[id] ?? [window(1, 20), window(1, 20)])[1] }])),
			rows: ids.flatMap((series) => rowsFor(series, grain, scenario.period, served[series])),
			definitions: [],
		} });
	});
}

async function atRest(page: Page, target: Locator) {
	await page.waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== 'running'
		|| animation.effect?.getTiming().iterations === Infinity));
	let last = '';
	await expect.poll(async () => {
		const now = JSON.stringify(await target.boundingBox());
		const still = now === last;
		last = now;
		return still;
	}, { message: 'the captured element has stopped moving' }).toBe(true);
}

async function enter(page: Page, request: APIRequestContext, testInfo: TestInfo, theme: 'light' | 'dark', scope: 'all' | 'project') {
	const suffix = `${testInfo.project.name}-${Date.now()}`;
	const project = await createProject(request, `Key figures ${suffix}`);
	const agentTitle = `Key figures agent ${suffix}`;
	const agentId = await createAgent(request, agentTitle, project.id);
	await serve(page, project, agentId, agentTitle);
	await page.addInitScript((setting) => localStorage.setItem('corsia_theme', setting), theme);
	await loginByUrl(page, { waitForComposer: false });
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	await expect(page.locator('.measurement-page')).toBeVisible({ timeout: 20_000 });
	if (scope === 'project') {
		await page.getByRole('button', { name: /Measurement scope:/ }).click();
		await page.getByRole('combobox', { name: 'Search projects and agents' }).fill(project.name);
		await page.getByRole('option', { name: new RegExp(`^${project.name}`) }).first().click();
		await expect(page.getByRole('button', { name: `Measurement scope: ${project.name}`, exact: true })).toBeVisible();
	}
	const row = page.locator('section.kpis');
	await expect(row.locator('[data-kpi="tokens"] strong')).toBeVisible({ timeout: 20_000 });
	return row;
}

const read = async (row: Locator, id: string) => (await row.locator(`[data-kpi="${id}"]`).innerText()).replace(/\s+/g, ' ').trim();
const ids = ['work', 'first-try', 'cost', 'minutes', 'tokens'];

async function expectTiles(row: Locator, scenario: Scenario) {
	for (const id of ids) expect(await read(row, id), `the ${id} tile`).toBe(scenario.expected[id]);
	expect(await row.textContent(), 'no internal names reach the reader').not.toMatch(/fresh_tokens|llm_usage|#\d|plan|undefined|NaN/i);
	await expect(row.getByRole('alert')).toHaveCount(0);
}

/** The tiles' boxes in reading order. */
const boxes = (row: Locator) => row.locator('[data-kpi]').evaluateAll((tiles) => tiles.map((tile) => {
	const box = tile.getBoundingClientRect();
	return { id: (tile as HTMLElement).dataset.kpi!, x: Math.round(box.x), y: Math.round(box.y), width: Math.round(box.width), bottom: Math.round(box.bottom), overflows: tile.scrollWidth > tile.clientWidth };
}));

/** The r2.7 type scale: label 12px/650, value 28px/750, sub-line 12px/400, on every tile. */
async function expectTypeScale(row: Locator, width: number) {
	const scale = await row.evaluate((section) => {
		const style = (selector: string) => [...new Set([...section.querySelectorAll(selector)].map((node) => { const s = getComputedStyle(node); return `${s.fontSize}/${s.fontWeight}`; }))];
		return { label: style('[data-kpi] .label'), value: style('[data-kpi] strong'), sub: style('[data-kpi] small') };
	});
	expect(scale, `at ${width} the type scale is the approved one`).toEqual({ label: ['12px/650'], value: ['28px/750'], sub: ['12px/400'] });
}

/** A value is a number and a short unit: it renders on one line at every width, never a wrapped sentence. */
async function expectOneLineValues(row: Locator, width: number) {
	const values = await row.locator('[data-kpi] strong').evaluateAll((nodes) => nodes.map((node) => {
		const style = getComputedStyle(node);
		return { text: node.textContent, height: node.getBoundingClientRect().height, line: parseFloat(style.lineHeight) };
	}));
	expect(values.length, 'five values').toBe(5);
	for (const value of values) expect(value.height, `at ${width} "${value.text}" is one line`).toBeLessThanOrEqual(value.line + 1);
}

const shot = (testInfo: TestInfo, name: string) => (process.env.SHOT_DIR ? `${process.env.SHOT_DIR}/${name}` : testInfo.outputPath(name));
async function capture(page: Page, row: Locator, testInfo: TestInfo, width: number, name: string) {
	await page.setViewportSize({ width, height: width === 393 ? 1500 : 1100 });
	await atRest(page, row);
	const tiles = await boxes(row);
	const box = await row.boundingBox();
	expect(box!.x + box!.width, `at ${width} the row ends inside the viewport`).toBeLessThanOrEqual(width);
	expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), `nothing scrolls sideways at ${width}`).toBeLessThanOrEqual(0);
	expect(tiles.map((tile) => tile.id), 'the tiles keep their reading order').toEqual(ids);
	expect(tiles.filter((tile) => tile.overflows), 'no tile clips its content').toEqual([]);
	if (width >= 1000) {
		expect(new Set(tiles.map((tile) => tile.y)).size, 'five tiles share one row').toBe(1);
		const regularWidths = tiles.slice(0, 4).map((tile) => tile.width);
		expect(Math.max(...regularWidths) - Math.min(...regularWidths), 'the first four tiles split the row evenly').toBeLessThanOrEqual(1);
		expect(tiles[4].width, 'the Tokens tile has its deliberate wide track').toBe(270);
	} else {
		expect(new Set(tiles.map((tile) => tile.x)).size, 'tiles stack in one column').toBe(1);
		expect(new Set(tiles.map((tile) => tile.width)).size, 'every tile is the full width').toBe(1);
		for (let i = 1; i < tiles.length; i += 1) expect(tiles[i].y, 'each tile sits below the one before').toBeGreaterThan(tiles[i - 1].bottom - 1);
	}
	await expectTypeScale(row, width);
	await expectOneLineValues(row, width);
	await row.evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await row.screenshot({ path: shot(testInfo, name) });
}

/** Scroll the Measurement page through its whole height at phone width and check no section title ever draws on the app header's title. */
async function expectNoTitleUnderHeader(page: Page) {
	await page.setViewportSize({ width: 393, height: 852 });
	const scroller = page.locator('.measurement-scroller');
	await expect(scroller).toBeVisible();
	const { height, client } = await scroller.evaluate((el) => ({ height: el.scrollHeight, client: el.clientHeight }));
	expect(height, 'the page is taller than the phone screen, so it scrolls').toBeGreaterThan(client);
	// One synchronous sweep, 8px at a time, so no title is stepped over. A title is drawn only where the scroller shows it, so its box is clipped to the scroller's.
	const overlaps = await scroller.evaluate((el) => {
		const header = document.querySelector('.shell-header .h-title')!.getBoundingClientRect();
		const view = el.getBoundingClientRect();
		const titles = [...document.querySelectorAll('.measurement-page h2, .measurement-page h3, .measurement-page h4')];
		const found: string[] = [];
		for (let top = 0; top <= el.scrollHeight; top += 8) {
			el.scrollTop = top;
			for (const node of titles) {
				const box = node.getBoundingClientRect();
				const shown = { top: Math.max(box.top, view.top), bottom: Math.min(box.bottom, view.bottom), left: Math.max(box.left, view.left), right: Math.min(box.right, view.right) };
				if (shown.bottom > shown.top && shown.right > shown.left && shown.bottom > header.top && shown.top < header.bottom && shown.right > header.left && shown.left < header.right) {
					found.push(`at scroll ${top} "${node.textContent?.trim()}" (${Math.round(shown.top)} to ${Math.round(shown.bottom)}) is drawn on the header title (${Math.round(header.top)} to ${Math.round(header.bottom)})`);
				}
			}
		}
		return found;
	});
	expect(overlaps.slice(0, 3), 'no section title is drawn on the header title at any scroll position').toEqual([]);
}

/** Scroll so the Cost section title sits at the top edge, where the app header is drawn. */
async function scrollTitleToHeader(page: Page) {
	await page.locator('#cost-title').evaluate((el) => {
		const scroller = el.closest('.measurement-scroller')!;
		scroller.scrollTop += el.getBoundingClientRect().top - 30;
	});
	await page.waitForTimeout(100);
}

test.describe('@flow:measurement-kpi-row @surface:measurement the key-figures row', () => {
	for (const scheme of ['light', 'dark'] as const) for (const scope of ['all', 'project'] as const) {
		test(`shows five tiles for both grains at phone and desktop width (${scheme}, ${scope})`, async ({ page, request }, testInfo) => {
			await page.emulateMedia({ colorScheme: scheme });
			const row = await enter(page, request, testInfo, scheme, scope);

			await expectTiles(row, days);
			await capture(page, row, testInfo, 393, `kpi-row-${scope}-${scheme}-days-393.png`);
			await capture(page, row, testInfo, 1280, `kpi-row-${scope}-${scheme}-days-1280.png`);

			await page.getByRole('button', { name: 'Weeks', exact: true }).click();
			await expect(row.locator('[data-kpi="work"]')).toContainText('Sep 7 to Oct 4');
			await expectTiles(row, weeksScenario);
			await capture(page, row, testInfo, 393, `kpi-row-${scope}-${scheme}-weeks-393.png`);
			await capture(page, row, testInfo, 1280, `kpi-row-${scope}-${scheme}-weeks-1280.png`);
		});
	}
	for (const scheme of ['light', 'dark'] as const) {
		test(`no section title scrolls under the header title at phone width (${scheme}, project)`, async ({ page, request }, testInfo) => {
			await page.emulateMedia({ colorScheme: scheme });
			await enter(page, request, testInfo, scheme, 'project');
			await page.setViewportSize({ width: 393, height: 852 });
			await scrollTitleToHeader(page);
			await page.screenshot({ path: shot(testInfo, `scrolled-${scheme}-393.png`) });
			await page.setViewportSize({ width: 1280, height: 800 });
			await scrollTitleToHeader(page);
			await page.screenshot({ path: shot(testInfo, `scrolled-${scheme}-1280.png`) });
			await expectNoTitleUnderHeader(page);
		});
	}
});
