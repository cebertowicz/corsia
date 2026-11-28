<script lang="ts">
	import type { Snippet } from 'svelte';

	/** The reply bar over an open document: who replied, where the person is in the replies, and the one action. */
	let { who, label, busy, action, problem }: {
		who: string;
		label: string;
		busy: boolean;
		action: Snippet;
		problem: Snippet;
	} = $props();
</script>

<section class="doc-bar" aria-label="Replies to your comments" aria-busy={busy}>
	<strong class="doc-who">{who}</strong>
	<div class="doc-line">
		<span class="doc-at" role="status">{label}</span>
		<span class="doc-done">{@render action()}</span>
	</div>
	{@render problem()}
</section>

<style>
	.doc-bar {
		display: flex; flex-direction: column; gap: var(--space-1);
		padding: var(--space-3) var(--space-5);
		border-bottom: 1px solid var(--border-subtle);
		background: var(--bg-primary); font-size: var(--text-footnote); color: var(--text-secondary);
	}
	.doc-who { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-semibold); overflow-wrap: anywhere; }
	.doc-line { display: flex; align-items: center; gap: var(--space-3); min-height: var(--space-7); }
	.doc-at { flex: 1; min-width: 0; font-size: var(--text-subhead); }
	.doc-done { flex: none; display: inline-flex; }
	:global(.activity.phone) .doc-bar { padding-inline: var(--space-3); }
	:global(.activity.phone) .doc-done :global(.btn) { min-height: var(--touch-target); }
	:global(.activity.phone) .doc-line { min-height: var(--touch-target); }
</style>
