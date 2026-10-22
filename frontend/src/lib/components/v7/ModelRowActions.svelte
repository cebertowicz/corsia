<script lang="ts">
	interface Props {
		showInfo: boolean;
		showCancel: boolean;
		showRetry: boolean;
		onInfo?: () => void;
		onCancel?: () => void;
		onRetry?: () => void;
	}

	const { showInfo, showCancel, showRetry, onInfo, onCancel, onRetry }: Props = $props();
</script>

<span class="action">
	{#if showInfo}
		<button type="button" class="ico-btn sm" aria-label="Info" onclick={(event) => { event.stopPropagation(); onInfo?.(); }}>
			<svg width="13" height="13" viewBox="0 0 24 24" fill="none">
				<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/>
				<path d="M12 11v5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
				<circle cx="12" cy="8" r="1" fill="currentColor"/>
			</svg>
		</button>
	{:else if showCancel}
		<button type="button" class="row-btn" onclick={(event) => { event.stopPropagation(); onCancel?.(); }}>Cancel</button>
	{:else if showRetry}
		<button type="button" class="row-btn danger" onclick={(event) => { event.stopPropagation(); onRetry?.(); }}>Retry</button>
	{/if}
</span>

<style>
	.action { grid-area: action; display: flex; align-items: center; justify-content: flex-end; min-width: calc(var(--space-6) * 2 + var(--space-1)); }
	/* The trailing glyph or word ends on the content edge, with every other Settings row's control;
	   the button keeps its hit area, which runs out past the edge instead. */
	.action > .ico-btn.sm { margin-right: calc((13px - 28px) / 2); }
	.action > .row-btn { margin-right: -10px; }
	.ico-btn {
		display: inline-flex; align-items: center; justify-content: center;
		border-radius: var(--radius-xs); color: var(--text-tertiary);
		transition: all var(--dur-tight) var(--ease-snap); background: transparent;
		border: 0; cursor: pointer;
	}
	.ico-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
	.ico-btn.sm { width: calc(var(--space-6) + var(--space-1)); height: calc(var(--space-6) + var(--space-1)); }
	.row-btn {
		display: inline-flex; align-items: center; gap: var(--space-1); padding: var(--space-1) calc(var(--space-2) + var(--space-1) / 2);
		border-radius: var(--radius-xs); background: transparent; color: var(--text-tertiary);
		font-size: var(--text-footnote); font-weight: 500; font-family: inherit;
		transition: all var(--dur-tight) var(--ease-snap); border: 0; cursor: pointer;
	}
	.row-btn:hover { background: var(--bg-tertiary); color: var(--text-primary); }
	.row-btn.danger:hover { color: var(--red); }
	@media (max-width: 480px) { .action { align-self: center; } }
</style>
