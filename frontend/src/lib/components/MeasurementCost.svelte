<script lang="ts">
	import type { Snippet } from 'svelte';
	import { coverageText, type CostState, type CostPeriod, type ExcludedRecords, type WorkspaceSubscriptionContext } from '$lib/stores/measurementCost';
	import MeasurementBarChart from './MeasurementBarChart.svelte';
	import { costBarPoints, costView } from './measurementCostRate';
	import { grainChart } from './measurementSize';
	import { periodName } from '$lib/stores/measurementPeriod';
	import { requestSettingsSection } from '$lib/stores/settingsNavigation';

	let { state: costState, onRetry, scoped = false, onOpenAllWork = () => {}, children }: { state: CostState; onRetry: () => void; scoped?: boolean; onOpenAllWork?: () => void; children?: Snippet } = $props();
	let view = $state<'equivalent' | 'actual'>('equivalent');

	function money(value: number | null, compact = false): string {
		if (value === null) return '—';
		return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', notation: compact ? 'compact' : 'standard', maximumFractionDigits: compact ? 1 : 2 }).format(value);
	}
	// An overage is recorded for one billing month, so it reads as that month, never as a date range.
	const billingMonth = new Intl.DateTimeFormat('en-US', { month: 'long', year: 'numeric', timeZone: 'UTC' });
	const monthOf = (from: string): string => billingMonth.format(new Date(`${from}T00:00:00Z`));
	function subscriptionParts(context: WorkspaceSubscriptionContext): string[] {
		if (context.subscription_dollars === null) return ['Not recorded'];
		const missing = context.subscription_unrecorded_accounts ?? 0;
		return missing > 0 ? [`${money(context.subscription_dollars)} recorded`, `${missing} ${missing === 1 ? 'account' : 'accounts'} not recorded`] : [`${money(context.subscription_dollars)} estimated`];
	}
	const day = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
	const requests = (count: number) => `${count.toLocaleString('en-US')} ${count === 1 ? 'request' : 'requests'}`;
	// Account prices live on each harness provider's Accounts tab, endpoint prices on an External provider's Advanced tab.
	function openPrices(accountsMissing: boolean): void { requestSettingsSection('providers', accountsMissing ? 'account-billing' : null); }
	// External endpoint billing has no section of its own: the link opens Providers, where each External provider's Advanced tab holds it.
	function openExternalBilling(): void { requestSettingsSection('providers', null); }
	const actualTitle = $derived(`What you actually paid${costState.data?.period ? `, ${periodName(costState.data.period.latest)}` : ''}`);
	const headline = $derived(costState.data?.period ? costView({ ...costState.data, period: costState.data.period }) : null);
	const rate = $derived(headline ? headline.current.perHour : null);
	/** The served series already opens at the first period with size-rated accepted work. */
	const served = $derived(costState.data?.series ?? []);
	const points = $derived(costBarPoints(costState.data?.series ?? []));
	const chart = $derived(grainChart(costState.data?.period ?? undefined, points, costState.data?.averagePeriods));
	const unit = $derived(chart.unit);
	/** The window the Actual figures cover is the latest comparison window, named by its dates like the title. */
	const periodText = $derived(costState.data?.period ? periodName(costState.data.period.latest) : 'this window');
	/** The sentence is true only when the first served period falls after the window's own first day: the lead-in before the window is read for the averages, so a series that opens on the window's first day may have begun earlier. */
	const startsAt = $derived(served.length && costState.data?.period && served[0].period > costState.data.period.range.from ? day.format(new Date(`${served[0].period}T00:00:00Z`)) : null);
	const group = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 0 });
	/** One sentence per unpriced reason, naming up to three models in the API's order. */
	const excludedLine = ({ text, models }: ExcludedRecords) => {
		const named = models.slice(0, 3).map((model) => `${model.label} (${group(model.records)})`);
		if (models.length > 3) named.push(`${group(models.length - 3)} more`);
		const list = named.length > 1 ? `${named.slice(0, -1).join(', ')} and ${named[named.length - 1]}` : named[0];
		return `${text.charAt(0).toUpperCase()}${text.slice(1)}: ${list}.`;
	};
	const usd0 = (value: number | null) => (value === null ? '–' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value));
</script>

<section class="card cost" aria-labelledby="cost-title">
	<div class="heading"><h3 id="cost-title">Cost per person-hour of work delivered</h3>
		<div class="toggle" aria-label="Cost view"><button class:active={view === 'equivalent'} aria-pressed={view === 'equivalent'} onclick={() => view = 'equivalent'}>API-equivalent</button><button class:active={view === 'actual'} aria-pressed={view === 'actual'} onclick={() => view = 'actual'}>Actual</button></div>
	</div>
	{#if costState.loading && !costState.data}<div class="skeleton" aria-label="Loading Cost"></div>
	{:else if costState.error && !costState.data}<p class="error" role="alert">{costState.error}<button onclick={onRetry}>Retry</button></p>
	{:else if costState.data}
		{#if headline}<p class="stat"><b>{rate === null ? '–' : money(rate)}</b> per person-hour of work delivered, {headline.nowText}, n {group(headline.current.sized)}, {headline.change}</p>{/if}
		{#if view === 'actual' && (scoped || !costState.data.actual.workspace_subscription)}
			<div class="actual"><h4>{actualTitle}</h4><p>Actual is recorded for the whole workspace, not this view.</p><p><button type="button" class="billing-link" onclick={onOpenAllWork}>Open All work</button></p><p class="note">The API-equivalent figures stay on the other view.</p></div>
		{:else if view === 'actual'}
			{@const workspaceSubscription = costState.data.actual.workspace_subscription!}
			{@const overageReadsNone = workspaceSubscription.overage_dollars === null && !workspaceSubscription.overlapping_overage.length}
			{@const priceGap = workspaceSubscription.subscription_coverage === 'absent' || workspaceSubscription.subscription_coverage === 'partial' || (workspaceSubscription.subscription_unrecorded_accounts ?? 0) > 0 || workspaceSubscription.overage_unrecorded === true}
			{@const actualComplete = costState.data.actual.complete && !priceGap}
			{@const overageNotRecorded = workspaceSubscription.overage_coverage === 'absent' || workspaceSubscription.overage_coverage === 'partial'}
			<div class="actual"><h4>{actualTitle}</h4><p>Actual: what you were charged, including workspace subscriptions.</p><strong>{actualComplete ? 'Actual cost complete' : 'Actual cost incomplete'} for {periodText}.</strong>
				{#if costState.data.actual.subscription_records}<p>{requests(costState.data.actual.subscription_records)} ran on {costState.data.actual.subscription_records === 1 ? 'a subscription' : 'subscriptions'}; their cost is in Workspace subscriptions below.</p>{/if}
				{#if costState.data.actual.unknown_charge_records}<p>No charge was recorded for {costState.data.actual.unknown_charge_records.toLocaleString('en-US')} pay-per-token usage records.{#if costState.data.actual.external_unset_records > 0}{' '}{costState.data.actual.external_unset_records.toLocaleString('en-US')} of them {costState.data.actual.external_unset_records === 1 ? 'is' : 'are'} on {costState.data.actual.external_unset_records === 1 ? 'an External endpoint' : 'External endpoints'} with no billing set. <button class="billing-link" onclick={openExternalBilling}>Set billing for External endpoints</button>{/if}</p>{/if}
				<div class="actual-values"><div><span>Direct charges</span><strong>{money(costState.data.actual.direct_api_dollars)}</strong></div><div><span>Workspace subscriptions (not split across projects)</span><strong>{#each subscriptionParts(workspaceSubscription) as part, index}{#if index}{' · '}{/if}<span class="keep">{part}</span>{/each}</strong></div><div><span>Overage</span><strong>{#if workspaceSubscription.overage_dollars !== null}{money(workspaceSubscription.overage_dollars)} recorded{/if}{#each workspaceSubscription.overlapping_overage as bill}<span class="bill">{money(bill.amount_cents / 100)} for {monthOf(bill.from_date)}, not split by {unit}</span>{/each}{#if workspaceSubscription.overage_dollars === null && !workspaceSubscription.overlapping_overage.length}{workspaceSubscription.overage_unrecorded ? 'Not recorded' : 'No overage recorded'}{/if}</strong></div></div>
				{#if workspaceSubscription.subscription_coverage === 'absent' || workspaceSubscription.subscription_coverage === 'partial'}<p>Subscription prices don't cover {periodText}. <button class="billing-link" onclick={() => openPrices((workspaceSubscription.subscription_unrecorded_accounts ?? 1) > 0)}>Set them in each provider's settings.</button></p>{:else if workspaceSubscription.subscription_coverage === 'complete'}{#if overageNotRecorded}{#if !overageReadsNone}<p>Overage is recorded for only part of this period.</p>{/if}{:else if actualComplete}<p>Subscriptions, overage and direct charges complete</p>{/if}{/if}<p class="note">The API-equivalent figures stay on the other view.</p></div>
		{:else}
			{#if headline}
				<h4>API-equivalent $ per person-hour of work delivered, {chart.rangeText}, by {unit}</h4>
				<div class="chartrow"><div class="chartcol">
				<MeasurementBarChart
					points={chart.points}
					axisTitle="API-equivalent $ per person-hour of work delivered"
					{unit}
					averageName={chart.averageName}
					caption={`API-equivalent dollars of every priced call divided by the person-hours the accepted work would take a capable person by hand; lower is better. n is size-rated accepted outcomes per ${unit}.${startsAt ? ` The series starts at ${startsAt}, the first ${unit} with size-rated accepted work.` : ''}`}
					minN={20}
					minNText="fewer than 20 outcomes"
					ariaLabel={`API-equivalent dollars per person-hour of work delivered, ${chart.rangeText}`}
					tickFormat={(value, decimals) => `$${value.toFixed(decimals)}`}
					valueFormat={(value) => value.toFixed(2)}
				/></div>
				<div class="spendcard"><span>Total spend, {headline.nowText}</span><strong>{usd0(headline.current.dollars)}</strong><small>API-equivalent, every priced call in the window</small><div class="hrs"><span>Hours of work delivered</span><b>{group(headline.current.hours)}</b></div></div></div>
			{:else}
				<p class="sentence">No usage records could be priced in this range.</p>
			{/if}
			<p class="caption">{coverageText(costState.data.coverage.priced_records, costState.data.coverage.usage_records)} in {periodText} are priced{#if costState.data.coverage.priced_records < costState.data.coverage.usage_records}; the rest are local, unlisted or unproven prices and are not in any dollar figure{/if}.{#each costState.data.excluded as excluded}{#if excluded.models.length}<br />{excludedLine(excluded)}{/if}{/each}</p>
		{/if}
		{#if costState.error}<p class="error" role="alert">{costState.error}<button onclick={onRetry}>Retry</button></p>{/if}
	{/if}
	{#if children && view === 'equivalent'}{@render children()}{/if}
</section>

<style>
	/* The Work per hour card's surface: Cost, its spend, Models and the pie sit in one card. */
	.card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: 0; padding: 14px; min-width: 0; }
	.cost { display:grid; gap:12px; } .heading { display:flex; align-items:center; gap:12px; flex-wrap:wrap; } h3,p { margin:0; } h3 { color:var(--text-primary); font-size:18px; font-weight:600; overflow-wrap:anywhere; } .caption { font-size:12px; font-style:italic; overflow-wrap:anywhere; } p,small { color:var(--text-secondary); font-size:var(--text-footnote); } .toggle { display:flex; border:1px solid var(--border-subtle); border-radius:var(--radius-xs); overflow:hidden; } button { font:inherit; color:var(--text-primary); background:var(--bg-primary); border:0; padding:5px 9px; cursor:pointer; } button.active { background:var(--text-primary); color:var(--bg-primary); font-weight:600; } .skeleton { height:160px; background:var(--bg-hover); border-radius:var(--radius-xs); } .error { color:var(--red-text); } .error button { border:1px solid var(--border-subtle); margin-left:8px; } .actual { display:grid; gap:8px; } .actual-values { display:grid; gap:4px; } .actual-values div { display:flex; align-items:baseline; justify-content:space-between; gap:12px; color:var(--text-secondary); font-size:var(--text-footnote); } .actual-values strong { color:var(--text-primary); font-weight:500; text-align:right; } .actual-values .bill { display:block; } .actual-values .keep { white-space:nowrap; } @media (max-width:480px) { .actual-values div { flex-direction:column; align-items:flex-start; gap:2px; } .actual-values strong { text-align:left; } } .billing-link { padding:0; color:var(--blue); background:transparent; text-decoration:underline; } .spendcard { background:var(--bg-secondary); border:1px solid var(--border-subtle); border-radius: 0; padding:12px; display:grid; gap:8px; min-width:0; } .stat { color:var(--text-secondary); font-size:var(--text-footnote); } .stat b { color:var(--text-primary); font-size:20px; margin-right:4px; } h4 { margin:0; color:var(--text-secondary); font-size:var(--text-footnote); font-weight:600; letter-spacing:.04em; text-transform:uppercase; } .spendcard span { color:var(--text-secondary); font-size:var(--text-footnote); } .spendcard strong { color:var(--text-primary); font-size:var(--text-title2); overflow-wrap:anywhere; } .hrs { display:flex; justify-content:space-between; gap:12px; color:var(--text-secondary); font-size:var(--text-footnote); } .hrs b { color:var(--text-primary); font-weight:600; } .chartrow { display:grid; gap:12px; grid-template-columns:minmax(0,1fr); } .chartcol { min-width:0; } @media(min-width:1100px) { .chartrow { grid-template-columns:minmax(0,1fr) 280px; align-items:start; } } @media(max-width:393px) { .heading { align-items:stretch; } .toggle { width:100%; } .toggle button { flex:1; } }
</style>
