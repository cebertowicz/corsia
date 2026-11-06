<script lang="ts">
	import type { Participant } from '$lib/stores/participants';
	import { compactRelLabel, formatDateTime } from '$lib/time';
	import MiddleName from '../MiddleName.svelte';

	let { author, at, createdAt, fullName = false }: { author: Participant; at: string; createdAt?: string; /** The thread is open: its author's whole name shows, wrapping. */ fullName?: boolean } = $props();

	const compact = $derived(compactRelLabel(createdAt, at));
	const full = $derived(createdAt ? formatDateTime(createdAt) : at);
	// A name that would be truncated elsewhere (MiddleName's 20 characters) drops under the header row at the card's full width.
	const stacked = $derived(fullName && author.name.length > 20);
</script>

<span class="ct-author" class:stacked title={author.name} aria-label={author.name}><MiddleName name={author.name} full={fullName} /></span>
<span class="ct-at full" title={full} aria-label={full}>{at}</span><span class="ct-at compact" title={full} aria-label={full}>{compact}</span>

<style>
	.ct-author { display: flex; flex: 1 1 auto; min-width: 0; font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--text-secondary); white-space: nowrap; }
	/* The header row wraps (CommentThread); the name goes last, on its own row, so time, count and actions stay together above it. */
	.ct-author.stacked { order: 1; flex: 0 0 100%; }
	.ct-at { flex: none; font-size: var(--text-caption); color: var(--text-tertiary); white-space: nowrap; }
	.ct-at.compact { display: none; }
	/* A phone shows the compact time so the name keeps its one line. */
	@media (pointer: coarse) { .ct-at.full { display: none; } .ct-at.compact { display: inline; } }
</style>
