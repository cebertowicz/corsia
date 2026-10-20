import { test } from '../support/test';
import { expect, type APIRequestContext } from '@playwright/test';
import { loginByUrl } from '../support/corsia';
import { agentsBody, createAgent, createProject } from '../support/measurement-fixture';
import { waitForMainSlideToSettle } from '../support/sidebar';

/**
 * The Models section at phone width.
 *
 * Why this flow exists: the shipped section put its four columns in a table with
 * `min-width: 560px` inside a horizontally scrolling box, so at 393px a reader saw
 * the model name and nothing else. Source, rate and share — the figures the section is
 * for — sat past the right edge with no cue that anything scrolled. A test that
 * only checks "nothing overflows the viewport" passes on that build, because the
 * overflow is contained inside the box. So the claim proved here is the one a
 * person can actually use: **the figures are inside the viewport, readable without
 * scrolling sideways**, plus the empty state and a failed read that recovers.
 *
 * Measurement is served from this file, exactly as the placement flow does, so the
 * three states depend on the fixture and not on how much history the disposable
 * account happens to carry.
 */

// The shapes a reader meets, one per served cost reason: partly priced rates (marked) with a
// percentage change and a new model, an alias whose version was never recorded (its rate a lower
// bound after a recording gap) with too few tasks before, an External row with no price, a local
// row with no list price, and the floor row holding only models with no tasks this week.
type SizeCell = { size: string; tasks: number; cost_per_task: number; cost_change: number | null; change_reason: string; first_try: number; partial: boolean; recording_gap: boolean };
const PAIRS: Array<{ model: string; label: string; tasks: number; failed_turns_per_100: number | null; spend_usd: number | null; recorded_cost_per_task: number | null; cost_class: string | null; rate_reason: string; cost_change: number | null; change_reason: string; previous_tasks: number; not_offered_now: boolean; other: boolean; includes_local: boolean; recording_gap: boolean; sizes: SizeCell[] }> = [
	{ model: 'claude-code/claude-opus-5-5', label: 'Opus 5.5', tasks: 360, failed_turns_per_100: 2.5, spend_usd: 900, recorded_cost_per_task: 2.5, cost_class: 'no_pricing', rate_reason: 'none', cost_change: -0.06, change_reason: 'none', previous_tasks: 350, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [{ size: 'S', tasks: 20, cost_per_task: 1.07, cost_change: null, change_reason: 'new', first_try: 0.94, partial: false, recording_gap: false }, { size: 'M', tasks: 20, cost_per_task: 3.09, cost_change: -0.07, change_reason: 'none', first_try: 0.92, partial: false, recording_gap: false }, { size: 'L', tasks: 20, cost_per_task: 8.56, cost_change: 0.12, change_reason: 'none', first_try: 0.85, partial: false, recording_gap: false }] },
	{ model: 'codex/gpt-6.1-sol', label: 'GPT-6.1 Sol', tasks: 120, failed_turns_per_100: 7.8, spend_usd: 240, recorded_cost_per_task: 1.99, cost_class: 'no_pricing', rate_reason: 'none', cost_change: null, change_reason: 'new', previous_tasks: 0, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [{ size: 'M', tasks: 20, cost_per_task: 1.94, cost_change: null, change_reason: 'new', first_try: 0.81, partial: true, recording_gap: false }] },
	{ model: 'claude-code/opus', label: 'opus (version not recorded)', tasks: 77, failed_turns_per_100: 3.1, spend_usd: 160, recorded_cost_per_task: 2.1, cost_class: 'priced', rate_reason: 'none', cost_change: null, change_reason: 'too_few_before', previous_tasks: 2, not_offered_now: false, other: false, includes_local: false, recording_gap: true, sizes: [] as SizeCell[] },
	{ model: 'claude-code/claude-sonnet-5-5', label: 'Sonnet 5.5', tasks: 60, failed_turns_per_100: 1.2, spend_usd: 55, recorded_cost_per_task: 0.95, cost_class: 'priced', rate_reason: 'none', cost_change: 0.08, change_reason: 'none', previous_tasks: 55, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [{ size: 'S', tasks: 20, cost_per_task: 0.48, cost_change: 0.05, change_reason: 'none', first_try: 0.97, partial: false, recording_gap: false }, { size: 'M', tasks: 20, cost_per_task: 1.31, cost_change: -0.2, change_reason: 'none', first_try: 0.9, partial: false, recording_gap: false }] },
	{ model: 'external@ns-example/qwen3.8-flash', label: 'Qwen 3.8 Flash', tasks: 40, failed_turns_per_100: 1.1, spend_usd: 45, recorded_cost_per_task: 1.12, cost_class: 'priced', rate_reason: 'none', cost_change: 0.02, change_reason: 'none', previous_tasks: 38, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [{ size: 'XS', tasks: 20, cost_per_task: 0.12, cost_change: -0.03, change_reason: 'none', first_try: 0.98, partial: false, recording_gap: false }, { size: 'XL', tasks: 20, cost_per_task: 41.2, cost_change: null, change_reason: 'new', first_try: 0.7, partial: false, recording_gap: false }] },
	{ model: 'external@ns-example/deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash', tasks: 22, failed_turns_per_100: null, spend_usd: null, recorded_cost_per_task: null, cost_class: 'no_pricing', rate_reason: 'unpriced', cost_change: null, change_reason: 'unpriced', previous_tasks: 19, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [] as SizeCell[] },
	{ model: 'omlx@srv/Fortissimo-8bit', label: 'Fortissimo', tasks: 15, failed_turns_per_100: 0.8, spend_usd: null, recorded_cost_per_task: null, cost_class: 'local', rate_reason: 'local', cost_change: null, change_reason: 'local', previous_tasks: 16, not_offered_now: false, other: false, includes_local: false, recording_gap: false, sizes: [] as SizeCell[] },
	{ model: '', label: 'Other models (under 14 tasks)', tasks: 0, failed_turns_per_100: 4.0, spend_usd: null, recorded_cost_per_task: null, cost_class: null, rate_reason: 'no_tasks', cost_change: null, change_reason: 'none', previous_tasks: 0, not_offered_now: false, other: true, includes_local: false, recording_gap: false, sizes: [] as SizeCell[] },
];
const TOKEN_SHARES = [
	{ model: 'claude-code/claude-opus-5-5', label: 'Opus 5.5', share_of_tokens: 0.42, tokens: 4200000000 },
	{ model: 'codex/gpt-6.1-sol', label: 'GPT-6.1 Sol', share_of_tokens: 0.21, tokens: 2100000000 },
	{ model: 'claude-code/opus', label: 'opus (version not recorded)', share_of_tokens: 0.11, tokens: 1100000000 },
	{ model: 'claude-code/claude-sonnet-5-5', label: 'Sonnet 5.5', share_of_tokens: 0.09, tokens: 900000000 },
	{ model: 'external@ns-example/deepseek-v4.1-flash', label: 'DeepSeek V4.1 Flash', share_of_tokens: 0.07, tokens: 700000000 },
	{ model: 'codex/gpt-5.6-terra', label: 'GPT-5.6 Terra', share_of_tokens: 0.05, tokens: 500000000 },
	// Served grouped: past the top six the backend sends one counted "Other" remainder.
	{ model: '', label: 'Other (4 more models)', share_of_tokens: 0.05, tokens: 500000000 },
];
/** The one label each served reason reads as; a dash is no figure and no cause to name. */
const REASON_TEXT: Record<string, string> = { no_tasks: '—', unpriced: 'unpriced', local: 'no list price' };
/** What the rate cell reads: a marked partial figure, a figure, or why there is none. */
function rateText(pair: (typeof PAIRS)[number]): string {
	if (pair.rate_reason !== 'none') return REASON_TEXT[pair.rate_reason];
	if (pair.recorded_cost_per_task === null) return '—';
	const amount = `${pair.recording_gap ? 'at least ' : ''}$${pair.recorded_cost_per_task.toFixed(2)}`;
	return `${amount}${pair.cost_class === 'no_pricing' ? '*' : ''}`;
}
/** The Other row reads 0 tasks here, so it is not drawn. */
const SHOWN = PAIRS.filter((pair) => !(pair.other && pair.tasks === 0));
const SIZE_COLUMNS = ['XS', 'S', 'M', 'L', 'XL', 'XXL'].filter((size) => PAIRS.some((pair) => pair.sizes.some((cell) => cell.size === size)));
/** What a row's cost cells read: one per size column, or its blended rate across them when it has no size cell. */
function costTexts(pair: (typeof PAIRS)[number]): string[] {
	if (!pair.sizes.length) {
		const rate = rateText(pair);
		return [pair.rate_reason === 'none' && pair.recorded_cost_per_task !== null ? `${rate} per accepted task, all sizes` : rate];
	}
	return SIZE_COLUMNS.map((size) => {
		const cell = pair.sizes.find((entry) => entry.size === size);
		if (!cell) return '—';
		const change = cell.change_reason === 'new' ? ' new' : cell.cost_change === null ? '' : ` ${cell.cost_change >= 0 ? '+' : ''}${Math.round(cell.cost_change * 100)}%`;
		return `${size} $${cell.cost_per_task.toFixed(2)}${cell.partial ? '*' : ''}${change} · ${Math.round(cell.first_try * 100)}% first try`;
	});
}
async function expectCosts(item: import('@playwright/test').Locator, pair: (typeof PAIRS)[number], message: string) {
	await expect(item.locator('td.blended, td.size'), message).toHaveText(costTexts(pair));
}
const FIRST_SHARE = `${(TOKEN_SHARES[0].share_of_tokens * 100).toFixed(1)}%`;

type ModelsOutcome = 'rows' | 'none' | 'fail-once';

function serveModels(mode: ModelsOutcome) {
	return async (page: import('@playwright/test').Page, project: { id: string; name: string }, agentId: string, agentName: string) => {
		let modelsCalls = 0;
		await page.route('**/api/measurement/**', async (route) => {
			const url = new URL(route.request().url());
			if (url.pathname.endsWith('/agents')) {
				await route.fulfill({ json: agentsBody(project, agentId, agentName, url) });
				return;
			}
			if (url.pathname.endsWith('/models')) {
				modelsCalls += 1;
				// A read that stays broken would not prove Retry does anything, so the
				// first attempt fails and the next one answers.
				if (mode === 'fail-once' && modelsCalls === 1) {
					await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"models unavailable"}' });
					return;
				}
				await route.fulfill({ json: { rows: mode === 'none' ? [] : PAIRS, min_tasks: 14, token_shares: mode === 'none' ? [] : TOKEN_SHARES, unmatched_failed_turns: 0, unresolved_token_share: 0, total_spend_usd: mode === 'none' ? 0 : 1400, period: { grain: 'days', timezone: 'UTC', current: '2026-10-05', range: { from: '2026-09-21', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } } } });
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
}

async function openMeasurement(page: import('@playwright/test').Page) {
	// At phone width the rail is collapsed, so Measurement lives behind the sidebar
	// toggle; on a wide viewport it is already on screen.
	const phone = (page.viewportSize()?.width ?? 0) <= 768;
	if (phone) {
		await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	}
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	await expect(page.locator('.shell-header .h-title')).toHaveText('All work', { timeout: 20_000 });
	if (phone) {
		// Following the link closes the rail and slides the main surface back; a box read
		// mid-slide sits up to the rail's width to the right, so measure only once it has landed.
		await expect(page.locator('aside.sidebar'), 'the phone rail closed after navigation').not.toHaveClass(/\bopen\b/);
		await waitForMainSlideToSettle(page);
	}
	// The page opens on All work, which shows every chart, Models inside the Cost card.
	const section = page.locator('section.models');
	await expect(section).toBeVisible({ timeout: 20_000 });
	return section;
}

/** A resize slides the rail in; capture only once that has finished and the element stopped moving. */
async function atRest(page: import('@playwright/test').Page, target: import('@playwright/test').Locator) {
	// Looping indicators (a loading pulse elsewhere on the page) never finish, so only finite ones count.
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

async function enter(page: import('@playwright/test').Page, request: APIRequestContext, suite: string, mode: ModelsOutcome, testInfo: import('@playwright/test').TestInfo, theme?: 'light' | 'dark') {
	// One fixture per project per attempt: the browser projects run the same flow, and
	// a rerun must not collide with a name an earlier attempt left behind.
	const suffix = `${testInfo.project.name}-${Date.now()}`;
	const project = await createProject(request, `Models 393 ${suite} ${suffix}`);
	const agentTitle = `Models 393 agent ${suffix}`;
	const agentId = await createAgent(request, agentTitle, project.id);
	await serveModels(mode)(page, project, agentId, agentTitle);
	if (theme) await page.addInitScript((setting) => localStorage.setItem('corsia_theme', setting), theme);
	await loginByUrl(page, { waitForComposer: false });
	if (theme) await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	return openMeasurement(page);
}

test.describe('@flow:measurement-models-393 @surface:measurement the Models section answers at phone width', () => {
	test.use({ viewport: { width: 393, height: 851 } });

	test('every pair shows its source and its share inside the viewport, with no sideways scroll', async ({ page, request }, testInfo) => {
		const section = await enter(page, request, 'rows', 'rows', testInfo);

		// The figures are the point of the section, so each pair is located by what it
		// says rather than by a markup hook: the item that names the pair must draw the
		// source, rate and share of that pair inside the 393px viewport. The same
		// assertion holds for the table row and for the stacked item on purpose, so the
		// flow measures what a reader can see, not which element the code chose.
		await expect(section.getByText('Other models (under 14 tasks)'), 'an Other row with no tasks is not drawn').toHaveCount(0);
		await expect(section.getByText('share the last table row'), 'nor is its sentence').toHaveCount(0);
		for (const pair of SHOWN) {
			const share = TOKEN_SHARES.find((item) => item.model === pair.model);
			const item = section.locator('tbody tr').filter({ hasText: pair.label }).first();
			await expect(item, `${pair.label} is drawn`).toBeVisible();
			if (share) await expect(section.getByText(`${(share.share_of_tokens * 100).toFixed(1)}%`).first(), `${pair.label} share is labelled`).toBeVisible();
			await expectCosts(item, pair, `${pair.label} names its rates by size, or its rate or why there is none`);
			await expect(item.locator('td.tasks'), `${pair.label} shows its task count`).toHaveText(`${pair.tasks}`);
			// A phone labels the pair: Share of spend (only for a priced row) beside Tasks, under the name.
			expect(await item.locator('td.tasks').evaluate((td) => getComputedStyle(td, '::before').content), `${pair.label} labels its task count`).toBe('"Tasks"');
			// The pair reads as words, not a one-character column (a desktop width share once leaked in).
			expect((await item.locator('td.tasks').boundingBox())!.width, `${pair.label} task cell is wide enough to read`).toBeGreaterThan(60);
			const priced = pair.spend_usd !== null;
			const spend = item.locator('td.spend');
			if (priced) await expect(spend, `${pair.label} shows its share of spend`).toBeVisible();
			else await expect(spend, `${pair.label} has no priced spend, so no Share pair`).toBeHidden();
			const box = await item.boundingBox();
			expect(box, `the pair ${pair.label} is drawn`).toBeTruthy();
			expect(box!.x + box!.width, `${pair.label} ends inside the 393px viewport`).toBeLessThanOrEqual(393);
		}
		expect(await section.textContent()).not.toMatch(/conversation|external_harness|claude-code\/|failed turns|too few turns|vs last week/i);
		await expect(section.getByText('* Some calls were unpriced; the figure covers priced calls only.'), 'one note explains the marker').toHaveCount(1);

		// Nothing about the section itself needs sideways scrolling to be read.
		const overflow = await section.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
		expect(overflow.scrollWidth, 'the section holds its content without sideways scroll')
			.toBeLessThanOrEqual(overflow.clientWidth);

		// A long model id wraps rather than being cut off: the whole identifier is
		// still in the text the reader is shown.
		await expect(section.getByText(PAIRS[1].label).first()).toBeVisible();

		// The pie shows its numbers: every slice has a legend row with its percent, inside the viewport.
		const legend = await section.locator('.legend li').evaluateAll((items) => items.map((item) => item.getBoundingClientRect().right));
		expect(legend).toHaveLength(TOKEN_SHARES.length);
		for (const right of legend) expect(right, 'every legend row ends inside the viewport').toBeLessThanOrEqual(393);
		const pie = await section.locator('.pie').boundingBox();
		expect(pie!.width, 'the pie is large enough to read').toBeGreaterThanOrEqual(200);
		await expect(section.getByText('Share of tokens by model, Sep 28 to Oct 4', { exact: true }), 'the pie says what it shares, for the window').toBeVisible();
		await expect(section.locator('.legend li').first(), 'each slice names its token total').toContainText('4.2B');
		await expect(section.locator('tbody tr').first().locator('td.spend'), 'a row names its share of spend').toHaveText('64.3%');
	});

	for (const scheme of ['light', 'dark'] as const) test(`the Models rows fit the viewport and no page-level scroller scrolls sideways (${scheme})`, async ({ page, request }, testInfo) => {
		await page.emulateMedia({ colorScheme: scheme });
		const section = await enter(page, request, 'tiles', 'rows', testInfo, scheme);
		for (const pair of SHOWN) {
			const item = section.locator('tbody tr').filter({ hasText: pair.label }).first();
			await expect(item, `${scheme}: ${pair.label} is drawn`).toBeVisible();
			const box = await item.boundingBox();
			expect(box!.x + box!.width, `${scheme}: ${pair.label} remains inside 393px`).toBeLessThanOrEqual(393);
			await expectCosts(item, pair, `${scheme}: ${pair.label} rates`);
		}
		// The shell's own pane scrolls, so the document alone can read clean while the
		// pane scrolls sideways. Measure every scroller a reader can drag. The agents
		// table keeps its own `.table-wrap` region on purpose; nothing else may scroll.
		const sideways = await page.evaluate(() => {
			const scrollers = [document.documentElement, document.body, ...Array.from(document.querySelectorAll<HTMLElement>('*'))]
				.filter((el) => el === document.documentElement || el === document.body || /auto|scroll/.test(getComputedStyle(el).overflowX));
			return scrollers
				.filter((el) => el.scrollWidth > el.clientWidth + 1 && !el.matches('.table-wrap'))
				.map((el) => `${el.tagName}.${String(el.className).slice(0, 40)} ${el.scrollWidth}>${el.clientWidth}`);
		});
		expect(sideways, 'no scroller other than an allowed table scrolls sideways').toEqual([]);
		await expect(section.getByText('Other (4 more models)', { exact: true }).first(), `${scheme}: token shares past the top six are one counted Other slice`).toBeVisible();
		const shot = (name: string) => (process.env.SHOT_DIR ? `${process.env.SHOT_DIR}/${name}` : testInfo.outputPath(name));
		// The shell scrolls its own pane under a sticky header, so a section taller than the
		// screen would be captured cut off and overlaid; a tall viewport keeps the width under test.
		await page.setViewportSize({ width: 393, height: 4000 });
		await atRest(page, section);
		await section.screenshot({ path: shot(`models-${scheme}-393.png`) });
		await page.setViewportSize({ width: 1280, height: 4000 });
		await atRest(page, section);
		const wide = await section.boundingBox();
		expect(wide!.x, `${scheme}: at 1280 the section starts inside the viewport`).toBeGreaterThanOrEqual(0);
		expect(wide!.x + wide!.width, `${scheme}: at 1280 the section ends inside the viewport`).toBeLessThanOrEqual(1280);
		await section.screenshot({ path: shot(`models-${scheme}-1280.png`) });
		// Mounted in the Cost card, the legend's token totals are drawn whole inside it, never cut at its right edge.
		const card = page.locator('section[aria-label="Cost"]');
		for (const width of [1280, 393]) {
			await page.setViewportSize({ width, height: 4000 });
			await atRest(page, card);
			const cardBox = (await card.boundingBox())!;
			for (const total of await section.locator('.legend em').all()) {
				const box = (await total.boundingBox())!;
				expect(cardBox.x + cardBox.width - (box.x + box.width), `${scheme} ${width}: a legend token total sits inside the Cost card`).toBeGreaterThanOrEqual(-0.5);
				const clipped = await total.evaluate((el) => el.scrollWidth > el.clientWidth);
				expect(clipped, `${scheme} ${width}: a legend token total is not clipped`).toBe(false);
			}
			// The capture keeps 24px of the page around the card, so an edge-touching total shows as one.
			const edge = 24;
			await page.screenshot({ path: shot(`models-in-cost-${scheme}-${width}.png`), clip: {
				x: Math.max(0, cardBox.x - edge), y: Math.max(0, cardBox.y - edge),
				width: Math.min(width, cardBox.width + 2 * edge), height: cardBox.height + 2 * edge } });
		}
	});

	test('a scope that recorded no model usage says so', async ({ page, request }, testInfo) => {
		const section = await enter(page, request, 'none', 'none', testInfo);
		await expect(section.getByText('No model activity in this scope.')).toBeVisible();
	});

	test('a failed read says so in the page\'s own words, and Retry serves the rows', async ({ page, request }, testInfo) => {
		const section = await enter(page, request, 'fail', 'fail-once', testInfo);
		const alert = section.getByRole('alert');
		await expect(alert).toBeVisible();
		// Raw transport text is not an explanation. The visible sentence names what
		// could not be loaded; the status code belongs in the log.
		await expect(alert).toContainText('Could not load models.');
		expect(await alert.textContent(), 'the status code stays out of the sentence').not.toMatch(/500/);
		await expect(section.getByText(FIRST_SHARE)).toHaveCount(0);

		await section.getByRole('button', { name: 'Retry', exact: true }).click();
		await expect(section.getByText(FIRST_SHARE).first()).toBeVisible({ timeout: 20_000 });
		const box = await section.getByText(FIRST_SHARE).first().boundingBox();
		expect(box!.x + box!.width).toBeLessThanOrEqual(393);
	});
});
