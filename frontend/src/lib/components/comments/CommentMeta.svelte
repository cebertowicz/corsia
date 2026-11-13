<script lang="ts">
	import type { Participant } from '$lib/stores/participants';
	import { compactRelLabel, formatDateTime } from '$lib/time';
	import MiddleName from '../MiddleName.svelte';

	let { author, at, createdAt, part }: { author: Participant; at: string; createdAt?: string; /** The name is the card header's first line, shortened in the middle when it is longer than the line (the full name is its title and accessible name); the time sits under it with the replies chip. */ part: 'author' | 'time' } = $props();

	const compact = $derived(compactRelLabel(createdAt, at));
	const full = $derived(createdAt ? formatDateTime(createdAt) : at);
</script>

{#if part === 'author'}
	<span class="ct-author" title={author.name} aria-label={author.name}><MiddleName name={author.name} /></span>
{:else}
	<span class="ct-at full" title={full} aria-label={full}>{at}</span><span class="ct-at compact" title={full} aria-label={full}>{compact}</span>
{/if}

<style>
	/* The name has the line to itself, so it is cut in the middle only when it is longer than the line, as in the reply list. */
	.ct-author { display: flex; flex: 1 1 auto; min-width: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); white-space: nowrap; }
	.ct-at { flex: none; font-size: var(--text-caption); color: var(--text-tertiary); white-space: nowrap; }
	.ct-at.compact { display: none; }
	/* A phone shows the compact time so the line under the name keeps its room. */
	@media (pointer: coarse) { .ct-at.full { display: none; } .ct-at.compact { display: inline; } }
</style>
