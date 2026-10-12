import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import MeasurementCost from './MeasurementCost.svelte';
import { decodeMeasurementCost, type CostState, type CostPeriod, type CostWindowFigures, type MeasurementCost as CostData, type WorkspaceSubscriptionContext } from '$lib/stores/measurementCost';
import { settingsActiveSection, settingsAnchorRequest, settingsOpenRequest } from '$lib/stores/settingsNavigation';

let component: Record<string, unknown> | null = null;
let host: HTMLElement | undefined;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); host = undefined; });

const period = (day: string, dollars: number | null, outcomes = 100, priced = 80, total = 100, hours = 2120, sized = 25, average: number | null = null): CostPeriod => ({
	period: day, dollars, priced_records: priced, usage_records: total, outcomes, per_outcome: dollars === null ? null : dollars / outcomes, sized, hours,
	per_hour: dollars === null || sized < 20 ? null : dollars / hours, average,
});
/** A pooled comparison window as the Cost payload serves it: named by the days it covers, with no `period` key. */
const pooled = ({ period: from, average: _average, ...figures }: CostPeriod): CostWindowFigures => ({ from, to: from, ...figures });
const first = period('2026-09-21', 5_300);
const second = period('2026-09-28', 10_600);
const workspace = (overrides: Partial<WorkspaceSubscriptionContext> = {}): WorkspaceSubscriptionContext => ({ subscription_dollars: null, overage_dollars: null, subscription_facts: 0, overage_facts: 0, subscription_coverage: 'absent', overage_coverage: 'absent', overlapping_overage: [], allocation: 'unavailable', ...overrides });
const data = (overrides: Partial<CostData> = {}): CostData => ({
	definition_version: 1,
	period: { grain: 'weeks', timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-07-13', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } },
	averagePeriods: 4, series: [first, second], latest: pooled(second), previous: pooled(first),
	coverage: { priced_records: 160, usage_records: 200 }, not_api_priced_records: 7,
	excluded: [{ kind: 'contract_unproven', text: 'usage records whose token counting format is not proven', records: 40 }],
	actual: { subscription_records: 12, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 4.2, complete: true, workspace_subscription: workspace() }, ...overrides,
});
const state = (overrides: Partial<CostState> = {}): CostState => ({ loading: false, error: null, scopeKey: 'project:none', grain: 'days', data: data(), ...overrides });
function render(overrides: Partial<CostState> = {}) { host = document.createElement('div'); document.body.append(host); component = mount(MeasurementCost, { target: host, props: { state: state(overrides), onRetry: () => {} } }); flushSync(); return host; }
const text = (element: HTMLElement) => element.textContent?.replace(/\s+/g, ' ') ?? '';

describe('MeasurementCost', () => {
	it('labels every figure as a known subtotal and states retained-record coverage and exclusions', () => {
		const content = text(render());
		expect(content).not.toContain('API-equivalent: list price of recorded tokens');
		expect(content).toContain('$5.00 per person-hour of work delivered, Sep 28 to Oct 4, n 25, +100% vs Sep 21 to Sep 27 (n 25)');
		expect(text(host!.querySelector('.spendcard') as HTMLElement)).toBe('Total spend, Sep 28 to Oct 4$10,600API-equivalent, every priced call in the windowHours of work delivered2,120');
		expect(content).not.toContain('per accepted outcome');
		expect(content).not.toContain('not in this rate');
		expect(content).toContain('160 of 200 usage records in Sep 28 to Oct 4 are priced; the rest are local, unlisted or unproven prices and are not in any dollar figure.');
		expect(content).not.toContain('Not priced:');
		expect(content).not.toContain('Not API-priced:');
	});
	const showActual = (actual: Partial<CostData['actual']> & { workspace_subscription?: WorkspaceSubscriptionContext } = {}) => {
		const markup = render({ data: data({ actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: workspace(), ...actual } }) });
		(markup.querySelectorAll('button')[1] as HTMLButtonElement).click(); flushSync();
		return markup;
	};
	const showActualWith = (overrides: Partial<CostState>, actual: Partial<CostData['actual']>) => {
		const markup = render({ ...overrides, data: data({ actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: true, workspace_subscription: workspace(), ...actual } }) });
		(markup.querySelectorAll('button')[1] as HTMLButtonElement).click(); flushSync();
		return markup;
	};
	it('shows only a pointer to All work for Actual in a scoped view', () => {
		let opened = 0;
		host = document.createElement('div'); document.body.append(host);
		component = mount(MeasurementCost, { target: host, props: { state: state(), onRetry: () => {}, scoped: true, onOpenAllWork: () => { opened += 1; } } }); flushSync();
		(host.querySelectorAll('button')[1] as HTMLButtonElement).click(); flushSync();
		expect(text(host)).toContain('Actual is recorded for the whole workspace, not this view.');
		expect(text(host.querySelector('.actual') as HTMLElement)).not.toMatch(/\$|Direct charges/);
		expect(text(host.querySelector('.stat') as HTMLElement)).toContain('per person-hour of work delivered');
		(host.querySelector('.actual button') as HTMLButtonElement).click();
		expect(opened).toBe(1);
		expect(text(host.querySelector('.actual button') as HTMLElement)).toBe('Open All work');
	});
	it('heads the Actual view with what you actually paid in the latest period, and points back to the API-equivalent view', () => {
		const markup = showActual({ direct_api_dollars: 4.2 });
		expect(text(markup.querySelector('.actual h4') as HTMLElement)).toBe('What you actually paid, Sep 28 to Oct 4');
		expect(text(markup.querySelector('.actual .note') as HTMLElement)).toBe('The API-equivalent figures stay on the other view.');
	});
	it('reads Actual as incomplete while subscription prices are not recorded, beside unallocated subscription usage', () => {
		const markup = showActual({ subscription_records: 12, direct_api_dollars: 4.2 });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(text(markup)).not.toContain('Actual spend is');
		expect(text(markup)).toContain('12 requests ran on subscriptions; their cost is in Workspace subscriptions below.');
		expect(text(markup)).not.toContain("can't be priced");
		expect(text(markup)).toContain('Direct charges$4.20');
		expect(text(markup)).not.toContain('Pay-per-token');
		expect(text(markup)).toContain('Workspace subscriptions (not split across projects)Not recorded');
	});
	it('names the period the unpriced requests are counted in, in the page grain', () => {
		const days = { grain: 'days' as const, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-09-07', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } };
		const markup = render({ grain: 'days', data: data({ period: days, actual: { ...data().actual, subscription_records: 1, complete: false } }) });
		(markup.querySelectorAll('button')[1] as HTMLButtonElement).click(); flushSync();
		const onDays = text(markup);
		expect(onDays).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(onDays).not.toContain('the last');
		expect(text(showActual({ subscription_records: 1, complete: false }))).toContain('1 request ran on a subscription; their cost is in Workspace subscriptions below.');
		expect(text(showActual({ subscription_records: 5000 }))).toContain('5,000 requests ran on subscriptions');
	});
	it('says what Actual covers once, and puts "not split across projects" only on the subscriptions label', () => {
		const content = text(showActual());
		expect(content).toContain('Actual: what you were charged, including workspace subscriptions.');
		expect(content.match(/not split across projects/g)).toHaveLength(1);
	});
	it('reports incomplete direct charges only for records without a charge', () => {
		const markup = showActual({ unknown_charge_records: 3, complete: false });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(text(markup)).toContain('No charge was recorded for 3 pay-per-token usage records.');
	});
	it('shows absent subscription prices as unknown and offers only the subscription prompt', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'absent', overage_coverage: 'absent' }) });
		expect(text(markup)).toContain('Workspace subscriptions (not split across projects)Not recorded');
		expect(text(markup)).toContain("Subscription prices don't cover Sep 28 to Oct 4. Set them in each provider's settings.");
		expect(text(markup)).not.toContain('Billing history');
		expect(text(markup)).not.toContain("Overage isn't recorded");
		expect(markup.querySelectorAll('.billing-link')).toHaveLength(1);
		const before = get(settingsOpenRequest);
		(markup.querySelector('.billing-link') as HTMLButtonElement).click(); flushSync();
		expect(get(settingsOpenRequest)).toBe(before + 1);
		expect(get(settingsActiveSection)).toBe('providers');
		expect(get(settingsAnchorRequest)).toBe('account-billing');
	});
	it('qualifies the subscription total by the accounts without a price, and never reads $0 for none', () => {
		const some = text(showActual({ workspace_subscription: workspace({ subscription_dollars: 20, subscription_unrecorded_accounts: 2, subscription_coverage: 'partial' }) }));
		expect(some).toContain('Workspace subscriptions (not split across projects)$20.00 recorded \u00b7 2 accounts not recorded');
		expect(text(showActual({ workspace_subscription: workspace({ subscription_dollars: 20, subscription_unrecorded_accounts: 1 }) }))).toContain('$20.00 recorded \u00b7 1 account not recorded');
		const none = text(showActual({ workspace_subscription: workspace({ subscription_dollars: null, subscription_unrecorded_accounts: 3 }) }));
		expect(none).toContain('Workspace subscriptions (not split across projects)Not recorded');
		expect(none).not.toContain('Workspace subscriptions (not split across projects)$0');
		expect(text(showActual({ workspace_subscription: workspace({ subscription_dollars: 20, subscription_unrecorded_accounts: 0 }) }))).toContain('$20.00 estimated');
	});
	it('says overage is not recorded once, in the Overage row, only when the backend reports it unrecorded', () => {
		const unrecorded = text(showActual({ workspace_subscription: workspace({ overage_unrecorded: true }) }));
		expect(unrecorded).toContain('OverageNot recorded');
		expect(unrecorded).not.toContain('Overage charges not included');
		expect(text(showActual({ workspace_subscription: workspace({ overage_unrecorded: false }) }))).not.toContain('OverageNot recorded');
	});
	it('reports no overage fact as not recorded, never as zero or a prompt, and labels the total for it', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_dollars: 12, subscription_facts: 1, subscription_coverage: 'complete', overage_coverage: 'absent' }) });
		expect(text(markup)).toContain('OverageNo overage recorded');
		expect(text(markup)).not.toContain('Overage$0.00');
		expect(text(markup)).not.toContain('Add it');
		expect(text(markup)).not.toContain('Subscriptions and direct charges covered');
		expect(text(markup)).not.toContain('Overage is recorded for only part of this period.');
		expect(text(markup)).not.toContain('overage not recorded');
		expect(text(markup)).not.toContain('direct charges complete');
		expect(markup.querySelector('.billing-link')).toBeNull();
	});
	it('renders an evidenced explicit zero as recorded and may then call the total complete', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_dollars: 0, overage_dollars: 0, subscription_facts: 1, overage_facts: 1, subscription_coverage: 'complete', overage_coverage: 'complete' }) });
		expect(text(markup)).toContain('Workspace subscriptions (not split across projects)$0.00 estimated');
		expect(text(markup)).toContain('Overage$0.00 recorded');
		expect(text(markup)).toContain('Subscriptions, overage and direct charges complete');
	});
	it('does not call the total complete while direct charges are incomplete', () => {
		const markup = showActual({ complete: false, unknown_charge_records: 1, workspace_subscription: workspace({ subscription_coverage: 'complete', overage_coverage: 'complete' }) });
		expect(text(markup)).not.toContain('Subscriptions, overage and direct charges complete');
	});
	it('shows an overage bill that only overlaps the range as recorded context, not added to it', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_dollars: 12, subscription_facts: 1, subscription_coverage: 'complete', overage_coverage: 'complete', overlapping_overage: [{ amount_cents: 1250, from_date: '2026-09-01', through: '2026-09-30' }] }) });
		expect(text(markup)).toContain('Overage$12.50 for September 2026, not split by week');
		expect(text(markup)).not.toContain('No overage recorded');
		expect(text(markup)).not.toContain('recorded for Sep 1–30');
		expect(text(markup)).not.toContain('Add it');
		expect(text(markup)).not.toContain("Overage isn't recorded");
	});
	it('lists each overlapping bill on its own line in the Overage value, after any contained amount', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_dollars: 12, overage_dollars: 5, subscription_facts: 1, overage_facts: 1, subscription_coverage: 'complete', overage_coverage: 'complete', overlapping_overage: [{ amount_cents: 1250, from_date: '2026-09-01', through: '2026-09-30' }, { amount_cents: 4000, from_date: '2026-10-01', through: '2026-10-31' }] }) });
		const value = markup.querySelectorAll('.actual-values > div')[2].querySelector('strong') as HTMLElement;
		expect(value.textContent).toContain('$5.00 recorded');
		expect(Array.from(value.querySelectorAll('.bill')).map((bill) => bill.textContent)).toEqual(['$12.50 for September 2026, not split by week', '$40.00 for October 2026, not split by week']);
		expect(text(markup)).not.toContain('No overage recorded');
	});
	it('does not name direct charges in the caption while they are incomplete', () => {
		const markup = showActual({ complete: false, unknown_charge_records: 2, workspace_subscription: workspace({ subscription_dollars: 12, subscription_facts: 1, subscription_coverage: 'complete', overage_coverage: 'absent' }) });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(text(markup)).toContain('OverageNo overage recorded');
		expect(text(markup)).not.toContain('Subscriptions covered');
		expect(text(markup)).not.toContain('Subscriptions and direct charges');
		expect(text(markup)).not.toContain('overage not recorded');
	});
	it('says overage is partly recorded while an overlapping bill is shown in the Overage value', () => {
		const bill = [{ amount_cents: 1250, from_date: '2026-09-01', through: '2026-09-30' }];
		const markup = showActual({ complete: false, unknown_charge_records: 2, workspace_subscription: workspace({ subscription_dollars: 12, subscription_facts: 1, subscription_coverage: 'complete', overage_coverage: 'absent', overlapping_overage: bill }) });
		expect(text(markup)).toContain('Overage is recorded for only part of this period.');
		expect(text(markup)).not.toContain('Subscriptions covered');
		expect(text(markup)).not.toContain('overage not recorded');
		expect(text(markup)).not.toContain('No overage recorded');
	});
	it('renders no caption when no subscription account is referenced, even with complete direct charges', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'none', overage_coverage: 'none' }) });
		expect(text(markup)).not.toContain('Subscriptions, overage and direct charges complete');
		expect(text(markup)).not.toContain('Subscriptions covered');
		expect(text(markup)).not.toContain('Subscriptions and direct charges');
	});
	it('reads incomplete with Not recorded in the Overage row when overage is unrecorded', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'complete', overage_coverage: 'absent', overage_unrecorded: true }) });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(text(markup)).toContain('OverageNot recorded');
		expect(text(markup)).not.toContain('No overage recorded');
	});
	it('reads incomplete when a subscription account has no price even if coverage is complete', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'complete', overage_coverage: 'complete', subscription_dollars: 20, subscription_unrecorded_accounts: 1 }) });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(text(markup)).not.toContain('Subscriptions, overage and direct charges complete');
	});
	it('says No overage recorded with no note when overage is not unrecorded', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'none', overage_coverage: 'none', overage_unrecorded: false }) });
		expect(text(markup)).toContain('OverageNo overage recorded');
	});
	it('does not show a subscription gap when no subscription account is referenced', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_coverage: 'none', overage_coverage: 'none' }) });
		expect(text(markup)).toContain('Actual cost complete for Sep 28 to Oct 4.');
		expect(text(markup)).not.toContain("Subscription prices don't cover Sep 28 to Oct 4.");
		expect(markup.querySelector('.billing-link')).toBeNull();
	});
	it('says so when no usage record could be priced, and withholds a rate under the minimum n', () => {
		expect(text(render({ data: data({ series: [], latest: null, previous: null }) }))).toContain('No usage records could be priced in this range.');
		unmount(component!); host?.remove();
		const thinPeriod = period('2026-09-28', 5_300, 100, 80, 100, 2120, 19);
		const thin = text(render({ data: data({ series: [thinPeriod], latest: pooled(thinPeriod), previous: null }) }));
		expect(thin).toContain('– per person-hour of work delivered, Sep 28 to Oct 4, n 19, n/a');
	});
	it('decodes an unknown weekly total without coercing it to zero and rejects a malformed count', () => {
		const unknown = { period: '2026-09-28', dollars: null, priced_records: 0, usage_records: 3, outcomes: 3, per_outcome: null };
		const body = { definition_version: 1, series: [unknown], latest: { ...unknown, from: '2026-09-28', to: '2026-09-28' }, previous: null, coverage: { priced_records: 0, usage_records: 3 }, not_api_priced_records: 0, excluded: [], actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: false, workspace_subscription: workspace() } };
		expect(decodeMeasurementCost(body).latest?.dollars).toBeNull();
		// A response without the per-hour fields reads as unknown, never as zero.
		expect(decodeMeasurementCost(body).series[0]).toMatchObject({ sized: 0, hours: 0, per_hour: null });
		expect(() => decodeMeasurementCost({ ...body, not_api_priced_records: 'many' })).toThrow('cost count shape');
	});
	it('fails visibly when the workspace subscription context is absent, and reads null as a scope without one', () => {
		const response = data();
		const { workspace_subscription: _missing, ...actual } = response.actual;
		expect(() => decodeMeasurementCost({ ...response, actual })).toThrow('actual cost shape');
		// Only All work serves the workspace bill; a project or agent view carries null.
		expect(decodeMeasurementCost({ ...response, actual: { ...actual, workspace_subscription: null } }).actual.workspace_subscription).toBeNull();
	});
	it('rejects malformed overlapping overage entries before rendering them', () => {
		const response = data({ actual: { subscription_records: 0, unknown_charge_records: 0, external_unset_records: 0, direct_api_dollars: 0, complete: false, workspace_subscription: workspace({ overlapping_overage: [{ amount_cents: 1250, from_date: '2026-09-01', through: '2026-09-30' }] }) } });
		for (const partial of [
			[{ amount_cents: 12.5, from_date: '2026-09-01', through: '2026-09-30' }],
			[{ amount_cents: Number.NaN, from_date: '2026-09-01', through: '2026-09-30' }],
			[{ amount_cents: -1, from_date: '2026-09-01', through: '2026-09-30' }],
			[{ amount_cents: 1, from_date: '2026-02-30', through: '2026-09-30' }],
			[{ amount_cents: 1, from_date: '2026-09-30', through: '2026-09-01' }],
			[{ amount_cents: 1, from_date: '2026-09-01' }],
		]) {
			expect(() => decodeMeasurementCost({ ...response, actual: { ...response.actual, workspace_subscription: { ...response.actual.workspace_subscription, overlapping_overage: partial } } })).toThrow('actual cost shape');
		}
	});
	it('says incomplete whenever pay-per-token charges are unknown, and complete only with none and no price gap', () => {
		const complete = { subscription_coverage: 'complete', overage_coverage: 'complete', subscription_dollars: 20, overage_dollars: 0, overage_facts: 1, subscription_facts: 1 } as const;
		const unknown = text(showActual({ complete: false, unknown_charge_records: 3, workspace_subscription: workspace(complete) }));
		expect(unknown).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		expect(unknown).not.toContain('Actual cost complete');
		const none = text(showActual({ workspace_subscription: workspace(complete) }));
		expect(none).toContain('Actual cost complete for Sep 28 to Oct 4.');
		// Subscription usage alone never makes Actual incomplete, and never appears as the reason.
		const subscriptionOnly = text(showActual({ subscription_records: 40, workspace_subscription: workspace(complete) }));
		expect(subscriptionOnly).toContain('Actual cost complete for Sep 28 to Oct 4.');
		expect(subscriptionOnly).toContain('40 requests ran on subscriptions');
		expect(subscriptionOnly).not.toContain('No charge was recorded');
	});
	it('names External endpoints with no billing set and links to provider settings', () => {
		const one = showActual({ complete: false, unknown_charge_records: 4, external_unset_records: 1 });
		expect(text(one)).toContain('No charge was recorded for 4 pay-per-token usage records. 1 of them is on an External endpoint with no billing set.');
		const link = [...one.querySelectorAll<HTMLButtonElement>('.billing-link')].find((button) => button.textContent === 'Set billing for External endpoints');
		expect(link).toBeDefined();
		const opened = get(settingsOpenRequest);
		link!.click();
		expect(get(settingsOpenRequest)).toBe(opened + 1);
		expect(get(settingsActiveSection)).toBe('providers');
		expect(get(settingsAnchorRequest)).toBeNull();
		unmount(component!); host?.remove();
		const many = text(showActual({ complete: false, unknown_charge_records: 4, external_unset_records: 3 }));
		expect(many).toContain('3 of them are on External endpoints with no billing set.');
		unmount(component!); host?.remove();
		const absent = showActual({ complete: false, unknown_charge_records: 4, external_unset_records: 0 });
		expect(text(absent)).not.toContain('External endpoint');
		expect([...absent.querySelectorAll('.billing-link')].map((button) => button.textContent)).not.toContain('Set billing for External endpoints');
	});
	it('reads an unpriced subscription account as not recorded with a way to set it, never as $0', () => {
		const markup = showActual({ workspace_subscription: workspace({ subscription_dollars: 20, subscription_facts: 1, subscription_unrecorded_accounts: 2, subscription_coverage: 'partial', overage_coverage: 'complete' }) });
		expect(text(markup)).toContain('Actual cost incomplete for Sep 28 to Oct 4.');
		const row = [...markup.querySelectorAll('.actual-values > div')][1] as HTMLElement;
		expect(text(row)).toContain('$20.00 recorded · 2 accounts not recorded');
		expect(text(row)).not.toContain('$0');
		const nothing = showActual({ workspace_subscription: workspace({ subscription_dollars: null, subscription_unrecorded_accounts: 1, subscription_coverage: 'absent' }) });
		const empty = [...nothing.querySelectorAll('.actual-values > div')][1] as HTMLElement;
		expect(text(empty)).toContain('Not recorded');
		expect(text(empty)).not.toContain('$0');
		expect([...nothing.querySelectorAll('.billing-link')].map((button) => button.textContent)).toContain("Set them in each provider's settings.");
	});
	it('decodes External endpoints with no billing set, reading an older payload without it as none', () => {
		const response = data({ actual: { subscription_records: 0, unknown_charge_records: 5, external_unset_records: 2, direct_api_dollars: 0, complete: false, workspace_subscription: workspace() } });
		expect(decodeMeasurementCost(response).actual.external_unset_records).toBe(2);
		const { external_unset_records: _omitted, ...older } = response.actual;
		expect(decodeMeasurementCost({ ...response, actual: older }).actual.external_unset_records).toBe(0);
		expect(() => decodeMeasurementCost({ ...response, actual: { ...response.actual, external_unset_records: 'many' } })).toThrow('cost count shape');
	});
	it('draws one bar per week of dollars per hour, from the served per-hour value', () => {
		const markup = render();
		expect([...markup.querySelectorAll('.cost .mob svg .vl')].map((label) => label.textContent)).toEqual(['2.50', '5.00']);
		expect([...markup.querySelectorAll('.cost .mob svg .nn')].map((label) => label.textContent)).toEqual(['n', '25', '25']);
	});
	it('draws the served series as it is and says where it starts', () => {
		const markup = render();
		expect(text(markup)).toContain('The series starts at Sep 21, the first week with size-rated accepted work.');
		expect(text(markup)).toContain('Hours of work delivered');
		// Two weeks serve no moving average, so no line is drawn and the caption does not name one.
		expect(text(markup)).not.toContain('moving average');
	});
	it('prints where the series starts only when it opens inside the shown window', () => {
		const dayRange = { grain: 'days' as const, timezone: 'America/Los_Angeles', current: '2026-10-05', range: { from: '2026-09-21', to: '2026-10-04' }, latest: { from: '2026-09-28', to: '2026-10-04' }, previous: { from: '2026-09-21', to: '2026-09-27' } };
		// The first served day is the window's own first day: the lead-in before it feeds the average, so the series began earlier.
		const earlier = render({ grain: 'days', data: data({ period: dayRange, series: [period('2026-09-21', 5_300, 100, 80, 100, 2120, 25, 0.1), second], previous: pooled(period('2026-09-21', 5_300)) }) });
		expect(text(earlier)).not.toContain('The series starts at');
		unmount(component!); component = null; host?.remove();
		const inside = render({ grain: 'days', data: data({ period: dayRange, series: [period('2026-09-25', 5_300), second], previous: pooled(period('2026-09-25', 5_300)) }) });
		expect(text(inside)).toContain('The series starts at Sep 25, the first day with size-rated accepted work.');
	});
	it('decodes the served latest and previous windows, which are named by the days they cover and carry no period key', () => {
		// The shape the backend serves (backend/api/measurement_cost.py): a series row has `period`; a window has `from` and `to`.
		const figures = { dollars: 40.19478, priced_records: 21, usage_records: 21, outcomes: 166, sized: 142, hours: 299.9, per_outcome: 0.2421372289, per_hour: 0.1340272758 };
		const served = { ...data(), series: [{ period: '2026-10-07', ...figures }], latest: { from: '2026-10-02', to: '2026-10-08', ...figures }, previous: { from: '2026-09-25', to: '2026-10-01', ...figures } };
		const decoded = decodeMeasurementCost(served);
		expect(decoded.latest).toMatchObject({ from: '2026-10-02', to: '2026-10-08', dollars: 40.19478, sized: 142, hours: 299.9 });
		expect(decoded.previous).toMatchObject({ from: '2026-09-25', to: '2026-10-01' });
		expect(() => decodeMeasurementCost({ ...served, latest: { to: '2026-10-08', ...figures } })).toThrow('cost window shape');
	});
	it('draws the served moving average over the bars, named from the grain, and reads it in the table', () => {
		const withAverage = data({ series: [period('2026-09-21', 5_300), period('2026-09-28', 10_600, 100, 80, 100, 2120, 25, 3.75)] });
		const periods = render({ data: withAverage });
		expect(periods.querySelectorAll('.cost .mob svg circle.ma')).toHaveLength(1);
		expect(periods.querySelector('.cost .lg')?.textContent).toContain('4-week moving average');
		expect([...periods.querySelectorAll('.cost table.sr-only tbody tr')].map((row) => [...row.querySelectorAll('th, td')].map((cell) => cell.textContent).join(' '))).toEqual(['2026-09-21 2.50 25 none', '2026-09-28 5.00 25 3.75']);
		unmount(component!); host?.remove();
		const days = render({ data: { ...withAverage, averagePeriods: 7, period: { ...withAverage.period!, grain: 'days' } } });
		expect(days.querySelector('.cost .lg')?.textContent).toContain('7-day moving average');
		expect(text(days)).not.toContain('the line is the 7-day moving average');
		unmount(component!); host?.remove();
		expect(render().querySelector('.cost .lg')?.textContent).not.toContain('moving average');
	});
	it('decodes the served moving average of a cost row: a number or null, an absent one as null, anything else rejected', () => {
		const row = { period: '2026-09-28', dollars: 10, priced_records: 1, usage_records: 1, outcomes: 1, per_outcome: 10, sized: 25, hours: 5, per_hour: 2 };
		const body = (average?: unknown) => ({ ...data(), series: [average === undefined ? row : { ...row, average }] });
		expect(decodeMeasurementCost(body(1.5)).series[0].average).toBe(1.5);
		expect(decodeMeasurementCost(body(null)).series[0].average).toBeNull();
		expect(decodeMeasurementCost(body()).series[0].average).toBeNull();
		expect(() => decodeMeasurementCost(body('1.5'))).toThrow('cost average shape');
		expect(() => decodeMeasurementCost(body(Number.NaN))).toThrow('cost average shape');
	});
	it('draws a period under the minimum n hollow with its spend over hours and low n, and withholds nothing else', () => {
		const thinPeriod = period('2026-09-21', 5_300, 100, 80, 100, 2120, 12);
		expect(thinPeriod.per_hour).toBeNull();
		const markup = render({ data: data({ series: [thinPeriod, period('2026-09-28', 10_600), period('2026-10-05', 9_000)], latest: pooled(period('2026-10-05', 9_000)), previous: pooled(period('2026-09-28', 10_600)) }) });
		const svg = markup.querySelector('.cost .mob svg')!;
		expect(svg.querySelectorAll('rect.bar.low')).toHaveLength(1);
		expect([...svg.querySelectorAll('text.vl')].map((label) => label.textContent)).toEqual(['2.50', '5.00', '4.25']);
		expect([...svg.querySelectorAll('text.lo')].map((label) => label.textContent)).toEqual(['low n']);
		expect(markup.querySelector('.cost .lg')?.textContent).not.toContain('too few');
		// A period with no priced spend, or no rated hours, stays unknown: no bar, never zero.
		const unknown = render({ data: data({ series: [period('2026-09-21', null), { ...thinPeriod, period: '2026-09-28', hours: 0 }, period('2026-10-05', 9_000)] }) });
		expect(unknown.querySelectorAll('.cost .mob svg rect.bar')).toHaveLength(1);
	});
	it('keeps charts inside the Cost section rather than a separate page surface', () => expect(render().querySelectorAll('.cost .chart')).toHaveLength(1));
	it('is one card, as Work per hour is, with the priced-records line as its caption', () => {
		const markup = render();
		const card = markup.querySelector('section.card.cost') as HTMLElement;
		expect(card.querySelector('h3')?.textContent).toBe('Cost per person-hour of work delivered');
		expect(card.querySelector('.spendcard')).not.toBeNull();
		expect(text(card.querySelector('.caption') as HTMLElement)).toBe('160 of 200 usage records in Sep 28 to Oct 4 are priced; the rest are local, unlisted or unproven prices and are not in any dollar figure.');
		expect(markup.querySelector('.missing')).toBeNull();
	});
});
