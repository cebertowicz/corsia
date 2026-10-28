<script lang="ts">
	import type { ContextUsage } from '$lib/stores/threads';
	import {
		activeCheckpoint,
		checkpointFreshnessLabel,
		latestCheckpoint,
		type CheckpointState,
	} from '$lib/stores/checkpoints';
	import { humanTokens } from '$lib/format';
	import CheckpointStatus from './CheckpointStatus.svelte';
	import ContextSheetSources from './ContextSheetSources.svelte';
	import { GROUP_COLOR, barLayout, type ScopeGroupId } from './contextSheetScopes';
	import {
		contextSheetSnapshotModel,
		contextSheetUsageSummary,
		type ContextSheetSourceRow,
	} from './contextSheetModel';

	let {
		usage,
		onOpenSource,
		checkpointState,
		loading = false,
		refreshFailed = '',
		threadId,
		threadLabel = '',
		onCheckpointNow,
		expandedGroups,
		onToggleGroup,
	}: {
		usage?: ContextUsage | null;
		onOpenSource?: (source: ContextSheetSourceRow) => void;
		checkpointState?: CheckpointState;
		loading?: boolean;
		/** Why the newest preview could not be fetched; the shown usage is then the last known. */
		refreshFailed?: string;
		threadId?: string | null;
		/** Names the thread these numbers belong to when more than one composer is on screen (#2269). */
		threadLabel?: string;
		onCheckpointNow?: () => void | Promise<void>;
		/** Opened source groups, kept by the owner of the open view; see ContextSheetSources. */
		expandedGroups?: Record<string, boolean>;
		onToggleGroup?: (groupId: string, open: boolean) => void;
	} = $props();

	const snapshot = $derived(usage ? contextSheetSnapshotModel(usage) : null);
	const usageSummary = $derived(usage ? contextSheetUsageSummary(usage, snapshot) : null);
	// One bar split by group, drawn only when the rows add up to the headline
	// exactly; otherwise the single bar stays at the headline and says so. No
	// minimum width: a tiny group stays tiny and is still named below.
	const bar = $derived(
		snapshot && usageSummary && usageSummary.usedTokens !== null
			? barLayout(snapshot.sourceGroups, usageSummary.usedTokens, usageSummary.windowTokens)
			: { segments: [], mismatch: null },
	);
	const segments = $derived(bar.segments);
	// Exact integers: rounded figures can read alike while the rows still miss the total.
	const exactTokens = (tokens: number) => Math.round(tokens).toLocaleString('en-US');
	const mismatchNote = $derived(
		bar.mismatch
			? `Source estimates add up to ${exactTokens(bar.mismatch.itemized)}, not the ${exactTokens(bar.mismatch.used)} total`
			: '',
	);
	// Checkpoints are automatic and Checkpoint Now is only an optional
	// shortcut, so the sheet never shows a boundary notice: nothing here is
	// something the user must act on (#1952).
	const checkpointEligible = $derived(Boolean(usage?.checkpoint_eligible));
	// The sheet opens on the last known usage while the preview loads (#1953).
	// That usage carries no eligibility answer, so the checkpoint action stays
	// hidden until the preview lands rather than flashing a wrong one.
	const refreshing = $derived(loading && Boolean(usage));
	const eligibilityPending = $derived(loading && usage?.checkpoint_eligible == null);
	const active = $derived(activeCheckpoint(checkpointState));
	const latest = $derived(latestCheckpoint(checkpointState));
	const visibleCheckpoint = $derived(active ?? latest);
	// An installed checkpoint with no new one possible reads as one line,
	// "Checkpointed N ago", not also CheckpointStatus's "Context saved" (#2050).
	const freshnessOnly = $derived(
		Boolean(checkpointState) && !active && !eligibilityPending && !checkpointEligible
			&& latest?.status === 'installed',
	);
	let checkpointActionBusy = $state(false);

	async function runCheckpointAction(action: () => void | Promise<void>) {
		if (checkpointActionBusy) return;
		checkpointActionBusy = true;
		try {
			await action();
		} finally {
			checkpointActionBusy = false;
		}
	}
</script>

<div class="sheet-section">
	<div class="ctx-summary-sticky">
		<div class="sheet-section-label">
			<span>Next turn context{#if threadLabel}<span class="ctx-thread-label"> · {threadLabel}</span>{/if}</span>
			{#if refreshing}
				<span class="ctx-updating" role="status">Updating…</span>
			{:else if refreshFailed && usage}
				<span class="ctx-updating" role="status" title={refreshFailed}>Could not refresh</span>
			{/if}
		</div>

		{#if loading && !usage}
			<div class="ctx-unmeasured">Loading context…</div>
		{:else if usageSummary && usageSummary.pct !== null && usageSummary.usedTokens !== null}
			<div
				class="ctx-headline"
				title={usageSummary.tokenSource === 'estimated'
					? 'Estimated by CORSIA. The provider has not reported usage for the next turn.'
					: undefined}
			>
				{#if usageSummary.tokenSource === 'estimated'}
					<span class="ctx-estimated-label">Estimated</span>
				{/if}
				<span class="ctx-used mono">{humanTokens(usageSummary.usedTokens)}</span>
				<span class="ctx-of">
					of {humanTokens(usageSummary.windowTokens)}{usageSummary.countsAgainstBudget ? ' budget' : ''}
				</span>
				<span class="ctx-pct mono {usageSummary.level}">{usageSummary.pct}%</span>
			</div>

			{#if segments.length > 0}
				<ul class="ctx-bar ctx-bar-groups" aria-label="Context by source">
					{#each segments as segment (segment.id)}
						<li
							class="ctx-segment"
							style={`width: ${segment.share}%; background: ${GROUP_COLOR[segment.id as ScopeGroupId]}`}
						>
							<span class="sr-only">{segment.name}: {humanTokens(segment.tokens)} tokens, {segment.percent}% of {humanTokens(usageSummary.windowTokens)}</span>
						</li>
					{/each}
				</ul>
			{:else}
				<div class="ctx-bar" aria-label={mismatchNote || undefined}>
					<div class="ctx-fill {usageSummary.level}" style={`width: ${Math.min(100, usageSummary.pct)}%`}></div>
				</div>
			{/if}
			{#if mismatchNote}
				<div class="ctx-bar-note" role="note">{mismatchNote}</div>
			{/if}
			{#if usageSummary.countsAgainstBudget && usageSummary.modelWindowTokens !== null}
				<div class="ctx-budget-row" title="CORSIA checkpoints this chat when it reaches this budget. Usage can briefly go over it while the checkpoint finishes.">
					<span>{usageSummary.overBudget ? 'Over budget — checkpoint pending' : 'Checkpoints at budget'}</span>
					<span class="mono">model window {humanTokens(usageSummary.modelWindowTokens)}</span>
				</div>
			{/if}
		{:else}
			<div class="ctx-unmeasured">
				<span>Usage unavailable</span>
				{#if usageSummary && usageSummary.windowTokens > 0}
					<span class="mono">
						{usageSummary.countsAgainstBudget ? 'budget' : 'window'}
						{humanTokens(usageSummary.windowTokens)}
					</span>
				{/if}
			</div>
		{/if}

	</div>

	{#if visibleCheckpoint || (checkpointState && !active && !eligibilityPending && ((checkpointEligible && onCheckpointNow) || latest))}
		<div class="ctx-checkpoint-row">
			{#if visibleCheckpoint && !freshnessOnly}
				<CheckpointStatus checkpoint={visibleCheckpoint} {threadId} />
			{/if}
			{#if eligibilityPending}
				<!-- eligibility is unknown until the preview lands -->
			{:else if checkpointState && !active && checkpointEligible && onCheckpointNow}
				<button
					type="button"
					class="ctx-checkpoint-now"
					disabled={checkpointActionBusy || checkpointState.loading}
					onclick={() => runCheckpointAction(onCheckpointNow)}
				>{latest?.status === 'failed' ? 'Try checkpoint again' : 'Checkpoint now'}</button>
			{:else if checkpointState && !active && !checkpointEligible && latest}
				<span class="ctx-checkpoint-idle">{checkpointFreshnessLabel(latest)}</span>
			{/if}
		</div>
	{/if}

	{#if snapshot}
		<ContextSheetSources {snapshot} {onOpenSource} {expandedGroups} {onToggleGroup} />
	{/if}
</div>

<style>
	.sheet-section {
		display: flex;
		flex-direction: column;
		max-height: min(72vh, 640px);
		padding: var(--space-3) var(--space-4);
		overflow: hidden;
	}
	.ctx-summary-sticky {
		position: sticky;
		top: 0;
		z-index: 1;
		padding-bottom: var(--space-1);
		background: inherit;
	}
	.sheet-section-label {
		display: flex; align-items: baseline; justify-content: space-between;
		font-size: var(--text-caption); font-weight: 700;
		text-transform: uppercase;
		letter-spacing: var(--ls-cap);
		color: color-mix(in srgb, var(--accent) 55%, var(--text-secondary));
		margin-bottom: var(--space-2);
	}
	.ctx-thread-label {
		font-weight: 400;
		text-transform: none;
		letter-spacing: 0;
		color: var(--text-secondary);
	}
	.ctx-updating {
		font-weight: 400;
		text-transform: none;
		letter-spacing: 0;
		color: var(--text-secondary);
	}
	.mono { font-family: var(--font-mono); }

	.ctx-headline {
		display: flex; align-items: baseline; gap: var(--space-2);
		margin-bottom: var(--space-2);
	}
	.ctx-used { font-size: var(--text-body); font-weight: 600; color: var(--text-primary); }
	.ctx-of { font-size: var(--text-footnote); color: var(--text-secondary); flex: 1; }
	.ctx-pct { font-size: var(--text-footnote); font-weight: 600; color: var(--text-secondary); }
	.ctx-pct.warn { color: var(--amber-text); }
	.ctx-pct.crit { color: var(--red-text); }
	.ctx-estimated {
		display: flex;
		align-items: baseline;
		gap: var(--space-2);
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	.ctx-estimated-label {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-size: var(--text-caption, var(--text-footnote));
		color: var(--text-secondary);
	}
	.ctx-unmeasured {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: var(--space-2);
		margin-bottom: var(--space-2);
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}

	.ctx-bar {
		height: var(--space-1); border-radius: var(--radius-2xs);
		background: var(--bg-primary);
		overflow: hidden;
		margin-bottom: var(--space-1);
	}
	ul.ctx-bar { display: flex; list-style: none; padding: 0; }
	.ctx-segment { height: 100%; flex: 0 0 auto; transition: width var(--dur-drawer) ease; }
	.ctx-bar-note { font-size: var(--text-footnote); color: var(--text-secondary); margin-bottom: var(--space-1); }
	.sr-only {
		position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
		overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
	}
	.ctx-fill {
		height: 100%; border-radius: var(--radius-2xs);
		background: var(--sky);
		transition: width var(--dur-drawer) ease;
	}
	.ctx-fill.warn { background: var(--amber); }
	.ctx-fill.crit { background: var(--red); }
	.ctx-budget-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-3);
		min-height: var(--space-6);
		margin-bottom: var(--space-1);
		color: var(--text-secondary);
		font-size: var(--text-footnote);
	}
	.ctx-budget-row .mono {
		white-space: nowrap;
	}

	.ctx-checkpoint-idle {
		flex-shrink: 0;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	/* Status text and the Checkpoint Now action share one tight row instead of
	   the status line and a standalone button row below it (#1954). */
	.ctx-checkpoint-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--space-2);
		min-height: var(--space-6);
		margin: var(--space-1) 0;
	}
	.ctx-checkpoint-row :global(.checkpoint-status) {
		flex: 1;
		margin: 0;
		min-width: 0;
	}
	.ctx-checkpoint-now {
		flex-shrink: 0;
		min-height: var(--space-6);
		padding: 2px var(--space-2);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-xs);
		background: none;
		color: var(--text-secondary);
		font: inherit;
		font-size: var(--text-footnote);
		font-weight: 550;
		cursor: pointer;
	}
	.ctx-checkpoint-now:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.ctx-checkpoint-now:disabled {
		opacity: 0.55;
		cursor: default;
	}

</style>
