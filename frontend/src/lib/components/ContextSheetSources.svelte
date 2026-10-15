<script lang="ts">
	import type {
		ContextSheetSnapshotModel,
		ContextSheetSourceRow,
	} from './contextSheetModel';
	import { GROUP_COLOR, type ScopeGroupId } from './contextSheetScopes';
	import { PENDING_EDIT_LABEL } from './contextSheetModel';

	/** The source-attribution half of the context sheet. Presentational only. */
	let {
		snapshot,
		onOpenSource,
		expandedGroups,
		onToggleGroup,
	}: {
		snapshot: ContextSheetSnapshotModel;
		onOpenSource?: (source: ContextSheetSourceRow) => void;
		/** The opened groups, by scope, kept by the owner of the open context view. */
		expandedGroups?: Record<string, boolean>;
		onToggleGroup?: (groupId: string, open: boolean) => void;
	} = $props();

	// Groups start collapsed and are remembered by scope, never by the label
	// shown for it. The owner of the open view keeps the record so it outlives
	// this list while a refresh has no snapshot; a standalone use passes none and
	// keeps its own, which is never read while an owner's record is passed.
	let localExpanded = $state<Record<string, boolean>>({});
	const isOpen = (groupId: string) => (expandedGroups ?? localExpanded)[groupId] === true;
	function setOpen(groupId: string, open: boolean) {
		if (expandedGroups) onToggleGroup?.(groupId, open);
		else localExpanded[groupId] = open;
	}
</script>

{#snippet sourceRow(source: ContextSheetSourceRow)}
	{@const detail = source.fullLabel && source.fullLabel !== source.label ? source.fullLabel : source.label}
	{#snippet main()}
		<div class="ctx-source-main">
			<span class="ctx-source-label" class:wrap={source.wrapLabel} title={detail}>{source.label}</span>
			{#if source.tags?.length}
				<span class="ctx-source-tag">{source.tags.join(' · ')}</span>
			{/if}
			{#if source.countLabel}
				<span class="ctx-source-count-label">{source.countLabel}</span>
			{:else if source.layer === 'tools' || source.layer === 'history'}
				<!-- Keeps the empty count cell so every Tools and History row ends at the same token edge. -->
				<span class="ctx-source-count-label" aria-hidden="true"></span>
			{/if}
			{#if source.deltaLabel}
				<span class="ctx-source-delta">{source.deltaLabel}</span>
			{/if}
			{#if source.tokenLabel}
				<span class="ctx-source-token mono">{source.tokenLabel}</span>
			{/if}
			{#if source.status !== 'included'}
				<span class="ctx-source-status {source.statusClass}">{source.statusLabel}</span>
			{/if}
			{#if source.pendingEdit}
				<span class="ctx-source-pending">{PENDING_EDIT_LABEL}</span>
			{/if}
		</div>
	{/snippet}
	{#if onOpenSource}
		<button
			type="button"
			class="ctx-source-row actionable"
			aria-label="{source.actionLabel || 'Show details'} for {detail}{source.pendingEdit ? `, ${PENDING_EDIT_LABEL}` : ''}"
			title={source.actionLabel || detail}
			onclick={() => onOpenSource?.(source)}
		>
			{@render main()}
		</button>
	{:else}
		<div class="ctx-source-row" title={detail}>
			{@render main()}
		</div>
	{/if}
{/snippet}

<div class="ctx-snapshot">
	{#if snapshot.sourceGroups.length > 0}
		<div class="ctx-attribution-label">Estimated source attribution</div>
		<div class="ctx-source-list" aria-label="Context sources">
			{#each snapshot.sourceGroups as group (group.id)}
				<details
					class="ctx-source-group"
					open={isOpen(group.id)}
					ontoggle={(event) => setOpen(group.id, event.currentTarget.open)}
				>
					<summary>
						<span class="ctx-source-summary-main">
							<span class="ctx-source-swatch" style={`background: ${GROUP_COLOR[group.id as ScopeGroupId]}`} aria-hidden="true"></span>
							<span>{group.category}</span>
							<span class="ctx-source-count">{group.sources.length}</span>
						</span>
						{#if group.countLabel || group.deltaLabel}
							<span class="ctx-source-group-meta">
								{#if group.deltaLabel}
									<span class="ctx-source-delta">{group.deltaLabel}</span>
								{/if}
								{#if group.countLabel}
									<span class="ctx-source-count-label">{group.countLabel}</span>
								{/if}
							</span>
						{/if}
						{#if group.tokenLabel}
							<span class="ctx-source-group-token mono">{group.tokenLabel}</span>
						{/if}
					</summary>
					<div class="ctx-source-group-body">
						{#if group.skills.length > 0}
							{#if group.instructions.length > 0}
								<div class="ctx-source-subhead"><span>Instructions</span><span class="mono">{group.instructionsTokenLabel}</span></div>
								{#each group.instructions as source (source.id)}
									{@render sourceRow(source)}
								{/each}
							{/if}
							<div class="ctx-source-subhead"><span>Skills</span><span class="mono">{group.skillsTokenLabel}</span></div>
							{#each group.skills as source (source.id)}
								{@render sourceRow(source)}
							{/each}
						{:else}
							{#each group.sources as source (source.id)}
								{@render sourceRow(source)}
							{/each}
						{/if}
					</div>
				</details>
			{/each}
		</div>
	{/if}
</div>

<style>
	.mono { font-family: var(--font-mono); }
	.ctx-snapshot {
		margin-top: var(--space-2);
		min-height: 0;
	}
	.ctx-attribution-label {
		margin-bottom: var(--space-1);
		font-size: var(--text-caption);
		color: var(--text-secondary);
	}
	.ctx-source-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		max-height: min(48vh, 420px);
		overflow-y: auto;
		padding-right: var(--space-1);
	}
	.ctx-source-group {
		border-radius: var(--radius-xs);
	}
	.ctx-source-group summary {
		display: grid;
		grid-template-columns: var(--space-3) max-content minmax(0, 1fr) max-content;
		align-items: center;
		gap: var(--space-2);
		cursor: pointer;
		list-style: none;
		min-height: var(--space-5);
		border-radius: var(--radius-xs);
		padding: 0 var(--space-1);
		font-size: var(--text-caption);
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: var(--ls-cap);
		color: var(--text-secondary);
	}
	.ctx-source-group summary::before {
		content: '›';
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: var(--space-3);
		color: var(--text-tertiary);
		font-size: var(--text-subhead);
		line-height: 1;
		transition: transform var(--dur-tight);
	}
	.ctx-source-group[open] summary::before {
		transform: rotate(90deg);
	}
	.ctx-source-group summary:hover {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.ctx-source-group summary:hover :is(.ctx-source-count, .ctx-source-count-label, .ctx-source-delta, .ctx-source-group-token) {
		color: var(--text-primary);
	}
	.ctx-source-group summary:focus-visible {
		outline: 2px solid var(--violet-line);
		outline-offset: 2px;
	}
	.ctx-source-group summary::-webkit-details-marker { display: none; }
	.ctx-source-summary-main {
		display: inline-flex;
		align-items: center;
		gap: var(--space-2);
		min-width: 0;
		justify-self: start;
	}
	.ctx-source-swatch {
		width: var(--space-2);
		height: var(--space-2);
		border-radius: var(--radius-2xs);
		flex: none;
	}
	.ctx-source-subhead {
		display: flex;
		justify-content: space-between;
		gap: var(--space-2);
		padding: var(--space-1) var(--space-1) 0;
		font-size: var(--text-caption);
		font-weight: 700;
		color: var(--text-secondary);
	}
	.ctx-source-tag {
		color: var(--text-secondary);
		font-size: var(--text-footnote);
		white-space: nowrap;
	}
	.ctx-source-count {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-xs);
		color: var(--text-secondary);
		padding: 0 var(--space-1);
	}
	.ctx-source-group-body {
		display: flex;
		flex-direction: column;
		gap: var(--space-1);
		margin: 0 0 var(--space-1) calc(var(--space-3) + var(--space-2));
	}
	.ctx-source-row {
		min-width: 0;
		padding: 0 var(--space-1);
		border-radius: var(--radius-xs);
		border: 0;
		background: transparent;
		color: inherit;
		font: inherit;
		text-align: left;
		width: 100%;
	}
	.ctx-source-row:hover {
		background: var(--bg-hover);
	}
	/* --text-secondary on --bg-hover is below 4.5:1 in light theme. */
	.ctx-source-row:hover :is(.ctx-source-label, .ctx-source-tag, .ctx-source-count-label, .ctx-source-delta, .ctx-source-token) {
		color: var(--text-primary);
	}
	.ctx-source-row.actionable {
		cursor: pointer;
	}
	.ctx-source-row.actionable:focus-visible {
		outline: 2px solid var(--violet-line);
		outline-offset: 2px;
	}
	.ctx-source-main {
		min-width: 0;
		max-width: 440px;
		display: grid;
		grid-template-columns: minmax(0, 1fr) max-content max-content max-content max-content max-content;
		gap: var(--space-2);
		align-items: center;
		min-height: var(--space-5);
	}
	.ctx-source-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-subhead);
		color: var(--text-secondary);
	}
	/* A label whose last words qualify the figure ("(estimated)") wraps instead of being cut off. */
	.ctx-source-label.wrap {
		overflow: visible;
		white-space: normal;
		overflow-wrap: anywhere;
	}
	.ctx-source-delta {
		color: var(--text-secondary);
		font-family: var(--font-mono);
		font-size: var(--text-footnote);
		white-space: nowrap;
	}
	.ctx-source-count-label {
		color: var(--text-secondary);
		font-family: var(--font-mono);
		font-size: var(--text-footnote);
		white-space: nowrap;
	}
	.ctx-source-group-token {
		color: var(--text-secondary);
		font-size: var(--text-footnote);
		white-space: nowrap;
		justify-self: end;
	}
	.ctx-source-group-meta {
		min-width: 0;
		display: inline-flex;
		align-items: center;
		justify-content: flex-end;
		gap: var(--space-2);
		overflow: hidden;
		white-space: nowrap;
	}
	.ctx-source-group-meta .ctx-source-count-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.ctx-source-token {
		color: var(--text-secondary);
		font-size: var(--text-footnote);
		white-space: nowrap;
	}
	.ctx-source-status {
		padding: 0 var(--space-1);
		border-radius: var(--radius-2xs);
		font-size: var(--text-footnote);
		line-height: 1.35;
		color: var(--text-secondary);
		background: var(--bg-primary);
		border: 1px solid var(--border-subtle);
	}
	.ctx-source-status.included {
		color: var(--text-secondary);
		border-color: color-mix(in srgb, var(--green) 34%, var(--border-subtle));
	}
	.ctx-source-status.omitted_limit,
	.ctx-source-status.omitted,
	.ctx-source-status.redacted {
		color: var(--amber-text);
		border-color: color-mix(in srgb, var(--amber) 34%, var(--border-subtle));
	}
	.ctx-source-status.blocked {
		color: var(--red-text);
		border-color: color-mix(in srgb, var(--red) 34%, var(--border-subtle));
	}
	/* --amber-text is 7.2:1 (dark) and 5.8:1 (light) on the hover surface, the lowest case. */
	.ctx-source-pending {
		grid-column: 1 / -1;
		color: var(--amber-text);
		font-size: var(--text-footnote);
	}
	.ctx-source-status.preview {
		color: var(--sky);
		border-color: color-mix(in srgb, var(--sky) 34%, var(--border-subtle));
	}

	@media (max-width: 460px) {
		.ctx-source-group summary {
			grid-template-columns: var(--space-3) max-content minmax(0, 1fr);
		}
		.ctx-source-group-meta {
			display: none;
		}
		.ctx-source-group-token {
			grid-column: 3;
		}
	}
</style>
