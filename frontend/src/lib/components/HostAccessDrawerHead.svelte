<script lang="ts">
	/**
	 * The head row every host-access sheet shares: its kind over the name, the status on the name's line,
	 * and Done at the trailing edge. The sheet is `headless`, so this row is the sheet's top and a drag
	 * handle, and nothing sits above it but the grabber.
	 */
	let {
		label,
		title,
		status = null,
		stale = false,
		onClose,
	}: {
		label: string;
		title: string;
		status?: string | null;
		stale?: boolean;
		onClose: () => void;
	} = $props();
</script>

<header class="decision-head" data-drawer-handle>
	<div class="head-label">{label}</div>
	<h2 title={title}>{title}</h2>
	{#if status}<span class="status" class:stale>{status}</span>{/if}
	<button class="head-done" type="button" onclick={onClose}>Done</button>
</header>

<style>
	/* Opaque, in the sheet's own fill, so nothing scrolled shows behind the title; a hairline under it,
	   as the decision drawer draws one, so content scrolling under it ends at a rule. */
	.decision-head {
		position: sticky;
		top: 0;
		z-index: 2;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto auto;
		grid-template-areas: 'label label label' 'title status done';
		align-items: center;
		column-gap: var(--space-3);
		padding: 0 0 var(--space-3);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
	}
	.head-label {
		grid-area: label;
		min-width: 0;
		overflow: hidden;
		color: var(--text-tertiary);
		font-size: var(--text-footnote);
		font-weight: var(--fw-semibold);
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	h2 {
		grid-area: title;
		min-width: 0;
		margin: var(--space-1) 0 0;
		overflow: hidden;
		color: var(--text-primary);
		font-size: var(--text-title3);
		font-weight: var(--fw-semibold);
		letter-spacing: -0.02em;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.status {
		grid-area: status;
		margin-top: var(--space-1);
		color: var(--amber-text);
		font-size: var(--text-footnote);
		font-weight: var(--fw-medium);
		white-space: nowrap;
	}
	.status.stale { color: var(--red-text); }
	/* The drawer's own Done (AdaptiveDrawer .drawer-done), moved onto the title's row. */
	.head-done {
		grid-area: done;
		margin: var(--space-1) calc(-1 * var(--space-2)) 0 0;
		padding: var(--space-1) var(--space-2);
		border: none;
		background: none;
		color: var(--accent-text);
		font: inherit;
		font-size: var(--text-subhead);
		font-weight: var(--fw-semibold);
		cursor: pointer;
	}
	.head-done:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: -2px;
		border-radius: var(--radius-sm);
	}
	/* Touch minimum for the sheet's only dismiss control, as the fill drawer sizes its touch controls. */
	@media (pointer: coarse) {
		.head-done { min-height: var(--touch-target); }
	}
</style>
