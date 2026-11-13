<script lang="ts">
	// The comments pill, one for every review surface (RSR-4): the document's header and the image viewer's bar open the
	// same drawer. It counts the comments shown by that surface's default list, never as a bare number: with their noun, or with
	// the comment icon alone on a canvas up to 600px or in an image bar while Send shows (an unread count keeps its noun). It carries
	// a dot while the person's comments and edits wait for Send. The host gives it its own button look through `class`.
	let { count, unread = 0, unsent = 0, loading = false, unknown = false, expanded = false, disabled = false, imageBadge = false, element = $bindable(), class: look = '', onOpen, ...data }: {
		/** Comments (threads) in the default list. */
		count: number;
		/** Threads with unread comments or replies. */
		unread?: number;
		/** The person's own comments and edits in the feedback batch that Send has not delivered. */
		unsent?: number;
		loading?: boolean;
		/** The document has not loaded, so a count would be a false claim. */
		unknown?: boolean;
		expanded?: boolean;
		disabled?: boolean;
		/** The image viewer uses a compact count on phones. */
		imageBadge?: boolean;
		element?: HTMLButtonElement;
		class?: string;
		onOpen: (opener: HTMLElement) => void;
	} & { [attribute: `data-${string}`]: string | boolean } = $props();

	// Comments, unread ones and what waits for Send each contribute their already-visible status to the pill name; what waits is
	// the person's comments and edits together, so it takes no noun of its own, as "unread" takes none.
	const unreadPhrase = $derived(`${unread} unread`);
	const unsentPhrase = $derived(`${unsent} waiting for Send`);
	const noun = $derived(count === 1 ? 'comment' : 'comments');
	const label = $derived(unknown ? 'Comments unavailable until the document opens' : `${count} ${noun}${unread ? `, ${unreadPhrase}` : ''}${unsent ? `, ${unsentPhrase}` : ''}`);
</script>

<button type="button" class={look} class:image-badge={imageBadge} {...data} bind:this={element} aria-expanded={expanded} {disabled}
	aria-label={loading ? 'Loading comments' : label}
	title={loading ? 'Comments are loading' : unknown ? 'Comments unavailable until the document opens' : undefined} onclick={(event) => onOpen(event.currentTarget)}>
	<span class="icon-anchor"><svg class="tool-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>{#if imageBadge && !unknown}<span class="badge-count" aria-hidden="true">{loading ? '…' : count}</span>{/if}</span>
	{#if !unknown}<span class="count">{loading ? '…' : count}</span>{#if !loading}{' '}<span class="count-unit" class:keep={unread > 0}>{noun}</span>{/if}{#if unread}{' '}<span class="unread">· {unreadPhrase}</span>{/if}{#if unsent}<span class="unsent-dot" aria-hidden="true"></span>{/if}{/if}
</button>

<style>
	.tool-icon { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; }
	.icon-anchor { position: relative; display: inline-flex; }
	.count { font-variant-numeric: tabular-nums; }
	.count-unit { color: var(--text-tertiary); }
	.unread { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
	.unsent-dot { width: var(--space-2); height: var(--space-2); border-radius: var(--radius-circle); background: var(--accent); }
	.badge-count { display: none; }
	@media (max-width: 48rem) {
		.image-badge { position: relative; width: var(--touch-target); min-width: var(--touch-target); padding: 0; }
		.image-badge .tool-icon { width: calc(var(--touch-target) / 2); height: calc(var(--touch-target) / 2); }
		.image-badge .badge-count { display: block; position: absolute; top: calc(var(--touch-target) / 2 - var(--space-1)); right: calc(1px - var(--space-2)); padding: 1px calc(var(--space-1) - 1px); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-primary); color: var(--text-primary); font-size: var(--text-caption); line-height: 1; font-weight: var(--fw-medium); font-variant-numeric: tabular-nums; }
		.image-badge .count, .image-badge .count-unit, .image-badge .unread { display: none; }
		.image-badge .unsent-dot { position: absolute; top: calc(var(--space-1) + 1px); right: var(--space-1); }
	}
	@container doc-canvas (max-width: 600px) {
		/* An unread count needs its noun: "3 · 3 unread" does not say what is counted. */
		.count-unit:not(.keep) { display: none; }
	}
</style>
