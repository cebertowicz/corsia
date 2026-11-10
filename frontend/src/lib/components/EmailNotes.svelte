<script lang="ts">
	// What the person must act on before sending, one amber line each directly above the From line, with the warning icon
	// and no box (DESIGN-SYSTEM §0.2 #2). An added recipient is marked where it sits in To or Cc; the quiet notes are drawn
	// under the header by `cm/email-header.ts`.
	let { lines }: { lines: string[] } = $props();
</script>

{#if lines.length}
	<ul class="email-loud" role="status">
		{#each lines as line, index (index)}
			<li>
				<svg class="loud-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
				<span>{line}</span>
			</li>
		{/each}
	</ul>
{/if}

<style>
	.email-loud {
		list-style: none; margin: 0;
		padding: var(--space-2) var(--doc-phone-inset, var(--space-3)) var(--space-1) var(--doc-phone-inset, var(--doc-rail, 44px));
		font-size: var(--text-footnote); line-height: var(--lh-normal); font-weight: var(--fw-semibold); color: var(--amber-text);
	}
	.email-loud li { display: flex; align-items: baseline; gap: var(--space-2); padding: 0 2px; overflow-wrap: anywhere; }
	.loud-icon { flex: 0 0 auto; align-self: center; }
</style>
