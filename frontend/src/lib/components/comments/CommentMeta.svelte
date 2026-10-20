<script lang="ts">
	import type { Participant } from '$lib/stores/participants';
	import { compactRelLabel, formatDateTime } from '$lib/time';
	import MiddleName from '../MiddleName.svelte';

	let { author, at, createdAt }: { author: Participant; at: string; createdAt?: string } = $props();

	const compact = $derived(compactRelLabel(createdAt, at));
	const full = $derived(createdAt ? formatDateTime(createdAt) : at);
</script>

<span class="ct-author" title={author.name} aria-label={author.name}><MiddleName name={author.name} /></span>
<span class="ct-at full" title={full} aria-label={full}>{at}</span><span class="ct-at compact" title={full} aria-label={full}>{compact}</span>

<style>
	.ct-author { display: flex; flex: 1 1 auto; min-width: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); white-space: nowrap; }

	.ct-at { flex: none; font-size: var(--text-caption); color: var(--text-tertiary); white-space: nowrap; }
	.ct-at.compact { display: none; }
	/* A phone shows the compact time so the name keeps its one line. */
	@media (pointer: coarse) { .ct-at.full { display: none; } .ct-at.compact { display: inline; } }
</style>
