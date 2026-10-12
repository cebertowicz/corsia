import { expect, test } from '@playwright/test';
import { loginByUrl } from '../support/corsia';
import { agentsBody, createAgent, createProject, mockAveragePeriods, mockPeriod, mockWindows, selectProjectScope } from '../support/measurement-fixture';

const latest = { period: '2026-09-28', dollars: 10_600, priced_records: 3_000, usage_records: 3_746, outcomes: 3746, per_outcome: 2.83, sized: 40, hours: 2_000, per_hour: 5.3, families: { 'Claude Code': 10_600 } };
const previous = { period: '2026-09-21', dollars: 5_300, priced_records: 900, usage_records: 1_425, outcomes: 1425, per_outcome: 3.72, sized: 30, hours: 1_500, per_hour: 3.53, families: { 'Claude Code': 5_300 } };
/** A pooled comparison window as the Cost payload serves it: named by the days it covers, with no `period` key. */
const pooled = ({ period: from, ...figures }: typeof latest) => ({ from, to: from, ...figures });
const partialActual = { subscription_records: 5_000, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 4.2, complete: true, workspace_subscription: { subscription_dollars: 22, overage_dollars: null, subscription_facts: 1, overage_facts: 0, subscription_coverage: 'partial', overage_coverage: 'absent', overlapping_overage: [], allocation: 'unavailable' } };
const noSubscriptionActual = { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: { subscription_dollars: null, overage_dollars: null, subscription_facts: 0, overage_facts: 0, subscription_coverage: 'none', overage_coverage: 'none', overlapping_overage: [], allocation: 'unavailable' } };

async function serveMeasurement(page: import('@playwright/test').Page, project: { id: string; name: string }, agent: { id: string; name: string }, actual = partialActual) {
	await page.route('**/api/measurement/**', async (route) => {
		const url = new URL(route.request().url());
		if (url.pathname.endsWith('/agents')) return route.fulfill({ json: agentsBody(project, agent.id, agent.name, url) });
		if (url.pathname.endsWith('/models')) return route.fulfill({ json: { rows: [] } });
		if (url.pathname.endsWith('/cost')) return route.fulfill({ json: { definition_version: 1, period: mockPeriod(url.searchParams.get('grain')), series: [previous, latest], latest: pooled(latest), previous: pooled(previous), coverage: { priced_records: 3_900, usage_records: 5_171 }, not_api_priced_records: 12, excluded: [{ kind: 'contract_unproven', text: 'usage records whose token counting format is not proven', records: 1_271 }], actual } });
		const ids = url.searchParams.get('ids')?.split(',').filter(Boolean) ?? [];
		return route.fulfill({ json: { period: mockPeriod(url.searchParams.get('grain')), average_periods: mockAveragePeriods(url.searchParams.get('grain')), windows: mockWindows(ids), rows: ids.flatMap((series) => ['2026-09-21', '2026-09-28'].map((period) => ({ period, series, value: series === 'outcomes' ? 20 : 1, average: series === 'outcomes' ? 20 : 1, n: 20, proxy: false, partial: false, comparable: true, break: null, dimension: null, thin: false }))), definitions: [] } });
	});
}

async function openCost(page: import('@playwright/test').Page, request: import('@playwright/test').APIRequestContext, testInfo: import('@playwright/test').TestInfo, actual = partialActual) {
	const suffix = `${testInfo.project.name}-${Date.now()}`;
	const project = await createProject(request, `Cost 393 ${suffix}`);
	const agent = { id: await createAgent(request, `Cost agent ${suffix}`, project.id), name: `Cost agent ${suffix}` };
	await serveMeasurement(page, project, agent, actual);
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
	await page.getByRole('link', { name: 'Measurement', exact: true }).click();
	await selectProjectScope(page, project.name);
	const section = page.locator('section[aria-label="Cost"]');
	await expect(section).toBeVisible({ timeout: 20_000 });
	return section;
}

test.describe('@flow:measurement-cost-393 @surface:measurement Cost is usable on a phone', () => {
	test.use({ viewport: { width: 393, height: 851 } });
	test.beforeEach(({}, testInfo) => test.skip(testInfo.project.name !== 'chromium-mobile', '393px evidence runs in the mobile browser project only.'));
	test('shows the approved API-equivalent and incomplete Actual states without sideways scrolling', async ({ page, request }, testInfo) => {
		const section = await openCost(page, request, testInfo);
		await expect(section.getByText('Total spend, Sep 28 to Oct 4')).toBeVisible();
		await expect(section.getByText('3,900 of 5,171 usage records in Sep 28 to Oct 4 are priced; the rest are local, unlisted or unproven prices and are not in any dollar figure.')).toBeVisible();
		await section.scrollIntoViewIfNeeded();
		let box = await section.boundingBox();
		expect(box!.x + box!.width).toBeLessThanOrEqual(393);
		let overflow = await section.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
		expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
		for (const theme of ['light', 'dark'] as const) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await page.screenshot({ path: testInfo.outputPath(`api-equivalent-393-${theme}.png`) });
		}
		await section.getByRole('button', { name: 'Actual', exact: true }).click();
		await expect(section.getByText('Actual is recorded for the whole workspace, not this view.')).toBeVisible();
		await expect(section.getByRole('button', { name: 'Open All work' })).toBeVisible();
		await expect(section.getByText('Direct charges', { exact: true })).toHaveCount(0);
		await section.scrollIntoViewIfNeeded();
		box = await section.boundingBox();
		expect(box!.x + box!.width).toBeLessThanOrEqual(393);
		overflow = await section.evaluate((el) => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth }));
		expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
		for (const theme of ['light', 'dark'] as const) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await page.screenshot({ path: testInfo.outputPath(`actual-393-${theme}.png`) });
		}
	});
});

test.describe('@flow:measurement-cost-desktop @surface:measurement Cost remains settled on desktop', () => {
	test.use({ viewport: { width: 1280, height: 900 } });
	test('keeps the API-equivalent summary and charts legible beside the Measurement controls in both themes', async ({ page, request }, testInfo) => {
		const section = await openCost(page, request, testInfo);
		await expect(section.getByText('Total spend, Sep 28 to Oct 4')).toBeVisible();
		await expect(section.getByRole('heading', { name: 'API-equivalent $ per person-hour of work delivered, Sep 21 to Oct 4, by day' })).toBeVisible();
		await section.scrollIntoViewIfNeeded();
		for (const theme of ['light', 'dark'] as const) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await page.screenshot({ path: testInfo.outputPath(`api-equivalent-1280-${theme}.png`) });
		}
		await section.getByRole('button', { name: 'Actual', exact: true }).click();
		await expect(section.getByText('Actual is recorded for the whole workspace, not this view.')).toBeVisible();
		for (const theme of ['light', 'dark'] as const) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			await page.screenshot({ path: testInfo.outputPath(`actual-1280-${theme}.png`) });
		}
	});
});
