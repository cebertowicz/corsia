<script lang="ts">
	import type { Participant } from '$lib/stores/participants';
	import { compactRelLabel, formatDateTime } from '$lib/time';
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import MiddleName from '../MiddleName.svelte';

	let { author, color, at, createdAt }: { author: Participant; color: string; at: string; createdAt?: string } = $props();

	const compact = $derived(compactRelLabel(createdAt, at));
	const full = $derived(createdAt ? formatDateTime(createdAt) : at);
	// The header is one line at every width: avatar, the name shortened in the middle (MiddleName), then the time.
</script>

<Avatar name={author.name} {color} hidden />
<span class="ct-who"><span class="ct-author" title={author.name} aria-label={author.name}><MiddleName name={author.name} /></span><span class="ct-at full" title={full} aria-label={full}>{at}</span><span class="ct-at compact" title={full} aria-label={full}>{compact}</span></span>

<style>
	/* The name and the time share one line. The time gives way by fit: when it doesn't fit beside the name at its floor it
	   wraps to a second line this box clips, so it stays in the reading order and adds no height. */
	.ct-who {
		display: flex; flex-wrap: wrap; align-content: flex-start; align-items: center; justify-content: space-between; column-gap: var(--ct-head-gap); row-gap: 0;
		flex: 1 1 auto; height: 1lh; overflow: clip; font-size: var(--text-footnote);
	}
	/* The name never goes below MiddleName's floor (its min-content), so nothing paints over it; it grows only to its whole length. */
	.ct-author { display: flex; flex: 1 1 0; max-width: max-content; font-weight: var(--fw-semibold); color: var(--text-secondary); white-space: nowrap; }
	/* The time may break between words only for its min-content, so a long date never raises the name's floor; on its line it is whole. */
	.ct-at { font-size: var(--text-caption); color: var(--text-tertiary); }
	.ct-at.compact { display: none; }
	/* A phone shows the compact time so the name keeps its one line. */
	@media (pointer: coarse) { .ct-at.full { display: none; } .ct-at.compact { display: inline; } }
	/* The header's parts give way in one order where the row is short: the avatar, then the time, then the reply count's noun.
	   1. The avatar, where the row is under its fit width: the name at its floor and a same-day time ('now', '5m', '3h') beside
	      the row's other parts. A card showing a reply count has the wider fit (the single-thread sheet hides its count). A New
	      mark is not counted, so on a New card the time may go first. [aria-hidden] outranks the avatar's own display rule.
	   2. The time, by fit (above).
	   3. The count's noun, where the row can't hold the name at its floor, the count and the actions: '12' with the comments
	      pill's mark; the noun stays in the accessible text and the count's title. Fit: '100 replies', with or without New.
	   A three-digit count's row has tighter gaps, so a New card's actions stay inside it below the avatar's fit, and above
	   that fit it keeps its noun. */
	:global(.ct-head:has(> .ct-reply-count[data-count-long])) { --ct-head-gap: 5px; }
	@media (pointer: coarse) {
		@container ct-head (width < 252px) { :global(.ct-head > .avatar[aria-hidden]) { display: none; } }
		@container ct-head (width < 306px) { :global(.ct-head:has(> .ct-reply-count):not(.sheet-thread .ct-head) > .avatar[aria-hidden]) { display: none; } }
		@container ct-head (width < 257px) { :global(.ct-head > .ct-reply-count > .ct-count-mark) { display: block; } :global(.ct-head > .ct-reply-count > .ct-count-unit) { position: absolute; inline-size: 1px; block-size: 1px; overflow: hidden; clip-path: inset(50%); } }
		@container ct-head (width < 288px) { :global(.ct-head:has(> .ct-new) > .ct-reply-count > .ct-count-mark) { display: block; } :global(.ct-head:has(> .ct-new) > .ct-reply-count > .ct-count-unit) { position: absolute; inline-size: 1px; block-size: 1px; overflow: hidden; clip-path: inset(50%); } }
	}
	@media (pointer: fine) {
		@container ct-head (width < 192px) { :global(.ct-head > .avatar[aria-hidden]) { display: none; } }
		@container ct-head (width < 246px) { :global(.ct-head:has(> .ct-reply-count):not(.sheet-thread .ct-head) > .avatar[aria-hidden]) { display: none; } }
		@container ct-head (width < 197px) { :global(.ct-head > .ct-reply-count > .ct-count-mark) { display: block; } :global(.ct-head > .ct-reply-count > .ct-count-unit) { position: absolute; inline-size: 1px; block-size: 1px; overflow: hidden; clip-path: inset(50%); } }
		@container ct-head (width < 228px) { :global(.ct-head:has(> .ct-new) > .ct-reply-count > .ct-count-mark) { display: block; } :global(.ct-head:has(> .ct-new) > .ct-reply-count > .ct-count-unit) { position: absolute; inline-size: 1px; block-size: 1px; overflow: hidden; clip-path: inset(50%); } }
	}
</style>
