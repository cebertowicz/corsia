<script lang="ts">
	// The comments pill, one for every review surface (RSR-4): the document's header and the image viewer's bar open the
	// same drawer, so they read the same. It counts open threads (resolved ones are not counted), names the unit on a
	// canvas wider than 600 px, carries a dot while messages wait to be sent, and an amber mark on its icon while
	// comments could not be placed. The host gives it its own button look through `class`.
	let { count, unsent = 0, unplaced = 0, loading = false, expanded = false, disabled = false, element = $bindable(), class: look = '', onOpen, ...data }: {
		/** Open threads. */
		count: number;
		/** Messages in the feedback batch that are not sent yet. */
		unsent?: number;
		/** Comments whose text could not be found, so they have no place in it. */
		unplaced?: number;
		loading?: boolean;
		expanded?: boolean;
		disabled?: boolean;
		element?: HTMLButtonElement;
		class?: string;
		onOpen: (opener: HTMLElement) => void;
	} & { [attribute: `data-${string}`]: string | boolean } = $props();

	// Threads, unplaced comments, and unsent messages each contribute their already-visible status to the pill name.
	const unplacedPhrase = $derived(`${unplaced} comment${unplaced === 1 ? '' : 's'} couldn't be placed`);
	const label = $derived(`${count} open thread${count === 1 ? '' : 's'}${unplaced ? `, ${unplacedPhrase}` : ''}${unsent ? `, ${unsent} message${unsent === 1 ? '' : 's'} not sent yet` : ''}`);
</script>

<button type="button" class={look} {...data} bind:this={element} aria-expanded={expanded} {disabled}
	aria-label={loading ? 'Loading comments' : label}
	title={loading ? 'Comments are loading' : unplaced ? unplacedPhrase : undefined} onclick={(event) => onOpen(event.currentTarget)}>
	<span class="icon-anchor"><svg class="tool-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>{#if unplaced}<span class="unplaced-dot" aria-hidden="true"></span>{/if}</span>
	<span class="count">{loading ? '…' : count}</span>{#if !loading}<span class="count-unit">{count === 1 ? 'thread' : 'threads'}</span>{/if}{#if unsent}<span class="unsent-dot" aria-hidden="true"></span>{/if}
</button>

<style>
	.tool-icon { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; }
	.icon-anchor { position: relative; display: inline-flex; }
	.unplaced-dot { position: absolute; top: -3px; right: -4px; width: 7px; height: 7px; border-radius: var(--radius-circle); background: var(--amber); box-shadow: 0 0 0 2px var(--bg-primary); }
	.count { font-variant-numeric: tabular-nums; }
	.count-unit { color: var(--text-tertiary); }
	.unsent-dot { width: var(--space-2); height: var(--space-2); border-radius: var(--radius-circle); background: var(--accent); }
	@container doc-canvas (max-width: 600px) {
		.count-unit { display: none; }
	}
</style>
