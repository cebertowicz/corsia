import { expect, test, type Locator, type Page, type APIRequestContext, type TestInfo } from '@playwright/test';
import { loginByUrl } from '../support/corsia';
import { agentsBody, createAgent, createProject, kpiBody, mockAveragePeriods, mockCostBody, mockPeriod, mockWindows, selectProjectScope } from '../support/measurement-fixture';

/**
 * The Biggest changes card: the largest moves for the worse and for the better between the
 * last two completed weeks, with the Agents table inside it and sortable by its headers.
 *
 * Why this flow exists: the card replaced a separate findings panel, so the claim proved
 * here is what a person sees at phone and desktop width in both themes: two ranked lists
 * with a fixed action line under each worse row, a dash (never zero) where there is no page
 * average, headers that sort the Agents table, and no sideways scroll on the page.
 */

const row = (id: string, name: string, kind: 'scope' | 'agent', measure: string, label: string, format: string, previous: number, latest: number, change: number, n: number, pageAverage: number | null, action: string) =>
	({ subject: { id, name, kind }, measure, label, format, previous, latest, change, n, overall: pageAverage, action });

const BODY = {
	minimum_n: 20,
	worse: [
		row('a1', 'Tech Debt', 'agent', 'minutes_per_outcome', 'Minutes per outcome', 'minutes', 0.9, 4.4, 3.62, 65, 1.8, 'Review its recent send-backs and how it asks you.'),
		row('a2', 'Providers & Inference', 'agent', 'dollars_per_hour', '$ per hour of work', 'dollars', 1.1, 1.9, 0.73, 103, 1.5, 'Check which size class and model drive the cost.'),
		row('a3', 'Bugs', 'agent', 'first_try', 'First try', 'percent', 0.92, 0.81, -0.12, 52, 0.89, 'Read its last rejected outcomes.'),
	],
	improved: [
		row('s', 'All work', 'scope', 'hours_per_hour', 'Work per hour of your attention', 'hours', 30, 92, 2.06, 1349, null, 'Keep doing what changed.'),
		row('s2', 'All work', 'scope', 'asks_per_outcome', 'Asks per outcome', 'ratio', 0.38, 0.12, -0.69, 1377, null, 'Keep doing what changed.'),
	],
};

async function serve(page: Page, project: { id: string; name: string }, agentId: string, agentName: string) {
	await page.route('**/api/measurement/**', async (route) => {
		const url = new URL(route.request().url());
		const grain = url.searchParams.get('grain');
		if (url.pathname.endsWith('/where-to-look')) {
			await route.fulfill({ json: { ...BODY, period: mockPeriod(grain) } });
			return;
		}
		if (url.pathname.endsWith('/key-figures')) {
			await route.fulfill({ json: { definition_version: 1, scope: { project: url.searchParams.get('project'), agent: null }, today: '2026-10-05', period: mockPeriod(grain), kpi: kpiBody } });
			return;
		}
		if (url.pathname.endsWith('/cost')) {
			await route.fulfill({ json: mockCostBody(grain) });
			return;
		}
		if (url.pathname.endsWith('/agents')) {
			const body = agentsBody(project, agentId, agentName, url);
			const base = body.rows[0];
			const more = [['b', 'Zed lane', 2, 0.5, 30], ['c', 'Amy lane', 3, 0.9, 10]] as const;
			body.rows = [base, ...more.map(([id, name, rank, first, outcomes]) => ({
				...base, rank, n: outcomes, agent: { ...base.agent, id: `${agentId}-${id}`, name },
				metrics: { ...base.metrics, first_try_acceptance: { ...base.metrics.first_try_acceptance, value: first } },
			}))];
			await route.fulfill({ json: body });
			return;
		}
		if (url.pathname.endsWith('/models')) {
			await route.fulfill({ json: { rows: [], min_tasks: 14, token_shares: [], unmatched_failed_turns: 0, unresolved_token_share: 0, total_spend_usd: 0 } });
			return;
		}
		const ids = url.searchParams.get('ids')?.split(',').filter(Boolean) ?? [];
		await route.fulfill({ json: {
			period: mockPeriod(grain), average_periods: mockAveragePeriods(grain), windows: mockWindows(ids),
			rows: ids.flatMap((series) => ['2026-08-03', '2026-08-10'].map((date) => ({
				period: date, series, value: series === 'outcomes' ? 20 : 1, average: series === 'outcomes' ? 20 : 1, n: 20, proxy: false,
				partial: false, comparable: true, break: null, dimension: null, thin: false,
			}))),
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

async function enter(page: Page, request: APIRequestContext, testInfo: TestInfo, theme: 'light' | 'dark') {
	const suffix = `${testInfo.project.name}-${Date.now()}`;
	const project = await createProject(request, `Biggest changes ${suffix}`);
	const agentTitle = `Biggest changes agent ${suffix}`;
	const agentId = await createAgent(request, agentTitle, project.id);
	await serve(page, project, agentId, agentTitle);
	await page.addInitScript((setting) => localStorage.setItem('corsia_theme', setting), theme);
	await loginByUrl(page, { waitForComposer: false });
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	await expect(page.locator('.shell-header .h-title')).toHaveText('All work', { timeout: 20_000 });
	await selectProjectScope(page, project.name);
	const card = page.locator('#measurement-where-to-look');
	await expect(card).toBeVisible({ timeout: 20_000 });
	await expect(card.locator('table.tb').first()).toBeVisible({ timeout: 20_000 });
	return card;
}

test.describe('@flow:measurement-where-to-look @surface:measurement the Biggest changes card', () => {
	for (const scheme of ['light', 'dark'] as const) {
		test(`lists the largest moves, sorts the Agents table and never scrolls sideways (${scheme})`, async ({ page, request }, testInfo) => {
			await page.emulateMedia({ colorScheme: scheme });
			const card = await enter(page, request, testInfo, scheme);

			await expect(card.getByRole('heading', { name: 'Biggest changes', level: 2 })).toBeVisible();
			await expect(card.getByText('Sep 28 to Oct 4 against Sep 21 to Sep 27, worst change first')).toBeVisible();
			await expect(card.getByRole('heading', { name: 'Largest changes for the worse' })).toBeVisible();
			await expect(card.getByRole('heading', { name: 'Improved' })).toBeVisible();
			const lists = card.locator('table.tb');
			await expect(lists).toHaveCount(2);
			await expect(lists.nth(0).locator('tbody tr')).toHaveCount(3);
			await expect(lists.nth(0).locator('tbody tr').first()).toContainText('Tech Debt');
			await expect(lists.nth(0).locator('tbody tr').first()).toContainText('0.9 to 4.4');
			await expect(lists.nth(0).locator('tbody tr').first()).toContainText('+362%');
			await expect(lists.nth(0).locator('.act').first()).toHaveText('Review its recent send-backs and how it asks you.');
			await expect(lists.nth(1).locator('tbody tr').first().locator('td.n').last(), 'a scope row shows a dash for Overall, never zero or blank').toHaveText('–');
			const first = lists.nth(0).locator('tbody tr').first();
			await expect(first.locator('td').nth(0).locator('b'), 'the subject is its own bold cell').toHaveText('Tech Debt');
			await expect(first.locator('.kind'), 'an agent carries no kind tag, as in r2.7').toHaveCount(0);
			await expect(first.locator('td').nth(1), 'the measure is its own cell').toContainText('Minutes per outcome');
			await expect(lists.nth(1).locator('.act'), 'an improved row carries no hint').toHaveCount(0);
			await expect(lists.nth(0).locator('thead th').last()).toHaveText('Overall');
			expect(await card.textContent(), 'no run-together subject, no failed turns, no old column name').not.toMatch(/All work work|failed turn|Page average/i);
			expect(await card.textContent(), 'no internal names reach the reader').not.toMatch(/fresh_tokens|llm_usage|#\d|plan/i);

			// The Agents table sits inside the card and sorts from its column headers.
			const agents = card.locator('.agents.embedded');
			await expect(agents).toBeVisible();
			await expect(card.getByLabel('Order')).toHaveCount(0);
			const names = () => agents.locator('tbody th .name').allTextContents();
			expect(await agents.locator('thead th').allTextContents(), 'the approved columns, no asks').toEqual([
				'Agent', 'Outcomes', 'First try', '$ per hour of work', 'Change in minutes per outcome, vs the previous window']);
			expect(await names(), 'the served order, no column sorted').toEqual([expect.stringContaining('Biggest changes agent'), 'Zed lane', 'Amy lane']);
			const zed = agents.locator('tbody tr').filter({ hasText: 'Zed lane' });
			await expect(zed.locator('td.n').nth(1)).toContainText('50%');
			await expect(zed.locator('td.n').nth(3)).toContainText('-50%');
			const hourly = agents.getByRole('button', { name: 'First try' });
			await hourly.click();
			await expect(hourly.locator('xpath=..')).toHaveAttribute('aria-sort', 'ascending');
			expect(await names()).toEqual(['Zed lane', expect.stringContaining('Biggest changes agent'), 'Amy lane']);
			await hourly.click();
			expect(await names()).toEqual(['Amy lane', expect.stringContaining('Biggest changes agent'), 'Zed lane']);
			await hourly.click();
			expect((await names())[0], 'a third click returns the served order').toContain('Biggest changes agent');

			// The page itself never scrolls sideways; the card holds its content at both widths.
			const shot = (name: string) => (process.env.SHOT_DIR ? `${process.env.SHOT_DIR}/${name}` : testInfo.outputPath(name));
			for (const width of [393, 1280] as const) {
				await page.setViewportSize({ width, height: 4000 });
				await atRest(page, card);
				const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
				expect(overflow, `${scheme}: no sideways page scroll at ${width}`).toBeLessThanOrEqual(0);
				const box = await card.boundingBox();
				expect(box!.x + box!.width, `${scheme}: at ${width} the card ends inside the viewport`).toBeLessThanOrEqual(width);
				const clipped = await card.locator('.tb-wrap').evaluateAll((wraps) => wraps.map((wrap) => wrap.scrollWidth - wrap.clientWidth));
				expect(Math.max(...clipped), `${scheme}: at ${width} every Overall column is fully inside its card`).toBeLessThanOrEqual(0);
				const subject = lists.nth(0).locator('tbody tr').first().locator('td.subj');
				if (width === 393) {
					// A phone shows the measure under the subject, in one cell; the measure column is gone.
					await expect(lists.nth(0).locator('th.measure')).toBeHidden();
					await expect(subject.locator('div.stacked')).toBeVisible();
					await expect(subject.locator('div.stacked')).toContainText('Minutes per outcome');
					// The Minutes agents table's headers wrap between whole words, never inside one.
					const minutes = page.locator('table[aria-labelledby="agents-minutes-title"]');
					await minutes.scrollIntoViewIfNeeded();
					const breaks = await minutes.locator('thead th').evaluateAll((cells) => cells.map((cell) => {
						const style = getComputedStyle(cell);
						return `${style.overflowWrap}/${style.wordBreak}`;
					}));
					expect(breaks, `${scheme}: Minutes table headers break only between words`).toEqual(Array(4).fill('normal/normal'));
					await minutes.screenshot({ path: shot(`minutes-agents-${scheme}-393.png`) });
					// The sort clicks above scroll the Agents box sideways to reach their header; read the box unscrolled.
					const region = agents.locator('.table-wrap');
					console.log(`${scheme} ${testInfo.project.name}: Agents box scrollLeft after the sort clicks: ${await region.evaluate((el) => el.scrollLeft)}`);
					await region.evaluate((el) => { el.scrollLeft = 0; });
					const starts = await region.evaluate((el) => {
						const box = el.getBoundingClientRect();
						return [...el.querySelectorAll('tbody th .name')].map((name) => name.getBoundingClientRect().left >= box.left - 0.5);
					});
					expect(starts, `${scheme}: every agent name starts inside the unscrolled box`).toEqual([true, true, true]);
					const lines = await region.locator('thead th').evaluateAll((cells) => cells.map((cell) => {
						const tops = new Set<number>();
						const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
						for (let node = walker.nextNode(); node; node = walker.nextNode()) {
							const range = document.createRange();
							range.selectNodeContents(node);
							for (const rect of range.getClientRects()) if (rect.width > 0) tops.add(Math.round(rect.top));
						}
						return tops.size;
					}));
					console.log(`${scheme} ${testInfo.project.name}: Agents header lines at 393: ${lines.join(', ')}`);
					expect(Math.max(...lines), `${scheme}: no Agents header runs past three lines at 393 (lines: ${lines.join(', ')})`).toBeLessThanOrEqual(3);
					await agents.screenshot({ path: shot(`agents-table-${scheme}-393.png`) });
					await region.evaluate((el) => { el.scrollLeft = el.scrollWidth; });
					await agents.screenshot({ path: shot(`agents-table-${scheme}-393-scrolled-end.png`) });
					await region.evaluate((el) => { el.scrollLeft = 0; });
				} else {
					await expect(subject.locator('div.stacked')).toBeHidden();
					const columns = await lists.evaluateAll((tables) => tables.map((table) =>
						[...table.querySelectorAll('thead th')].map((th) => Math.round(th.getBoundingClientRect().width))));
					expect(columns[1], `${scheme}: the worse and improved tables share column widths`).toEqual(columns[0]);
				}
				await card.screenshot({ path: shot(`where-to-look-${scheme}-${width}.png`) });
			}
		});
	}
});
