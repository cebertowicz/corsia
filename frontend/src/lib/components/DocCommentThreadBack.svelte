<script lang="ts">
	/** The comment list's thread view header: Back returns to the list where it was (docCommentThreadView). */
	let { onBack }: { onBack: () => void } = $props();
</script>

<div class="thread-back"><button type="button" class="drawer-back" data-drawer-back onclick={onBack}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>Back</button></div>

<style>
	.thread-back { flex: none; }
	.drawer-back { display: inline-flex; align-items: center; gap: var(--space-1); min-height: var(--space-6); padding: 0 var(--space-1); border: 0; border-radius: var(--radius-xs); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer; }
	.drawer-back:hover { color: var(--text-primary); }
	.drawer-back:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) { .drawer-back { min-height: var(--touch-target); } }
</style>
