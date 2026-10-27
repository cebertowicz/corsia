<script lang="ts">
	/**
	 * The transcript's one needs-you row: a lead mark, the title with an optional aside on its line, an
	 * optional state line, and a trailing mark. An open ask (`AttachmentRow`) and a person step
	 * (`PersonStepRow`) both render it, so their frame, type and states never drift apart. `needsYou`
	 * puts the open-ask frame on it (frosted card); without it the row is flat prose-width.
	 */
	import type { Snippet } from 'svelte';
	import type { HTMLAttributes } from 'svelte/elements';

	let {
		as = 'button',
		needsYou = false,
		tone = null,
		counted = false,
		title,
		lead,
		aside,
		twoLine = false,
		ctx,
		trailing,
		...rest
	}: {
		/** `button` when the row acts; `div` when it is only shown. */
		as?: 'button' | 'div';
		needsYou?: boolean;
		/** A retired (`gone`) or muted row reads quieter. */
		tone?: 'gone' | 'muted' | null;
		/** The state line holds a feedback count that wraps out of view rather than truncating. */
		counted?: boolean;
		title: string;
		lead: Snippet;
		/** Beside the title, never truncated: the time of an ask. */
		aside?: Snippet;
		/** The title wraps to a second line before it is cut, so a typical title reads whole on a phone. */
		twoLine?: boolean;
		ctx?: Snippet;
		trailing?: Snippet;
	} & Omit<HTMLAttributes<HTMLElement>, 'title' | 'children'> = $props();
</script>

<svelte:element
	this={as}
	{...rest}
	type={as === 'button' ? 'button' : undefined}
	class="tile row {as === 'button' ? 'press-tint' : 'static'}"
	class:needs-you={needsYou}
	class:gone={tone === 'gone'}
	class:muted={tone === 'muted'}
	title={title}
>
	{@render lead()}
	<span class="text">
		<span class="head">
			<span class="title" class:two-line={twoLine}>{title}</span>
			{#if aside}<span class="when">{@render aside()}</span>{/if}
		</span>
		{#if ctx}<span class="ctx" class:counted>{@render ctx()}</span>{/if}
	</span>
	{@render trailing?.()}
</svelte:element>

<style>
	.tile {
		display: flex;
		align-items: center;
		gap: var(--space-3);
		min-width: 0;
		width: 100%;
		padding: var(--space-2) var(--space-2) var(--space-2) calc(var(--space-1) / 2);
		border: 0;
		border-radius: var(--radius-sm);
		background: transparent;
		color: var(--text-primary);
		font: inherit;
		text-align: left;
		transition: background var(--dur-tight) var(--ease-snap);
	}
	/* An open ask is the same frosted card as the pinned question and the composer. It is a
	   needs-you object, not a worked artifact chip or a canvas review control. */
	.tile.needs-you { border: 1px solid var(--border); border-radius: var(--radius-xl); max-width: var(--needs-you-tile-max-width); background: var(--glass-thin); box-shadow: var(--glass-rim), var(--shadow-chip); }
	button.tile { cursor: pointer; }
	@media (hover: hover) { button.tile:hover { background: var(--bg-secondary); } }
	/* Inset: the message clips at the prose edge, so an outward ring would be cut off.
	   The row's 2 px left padding (always there, so focus moves nothing) is the room the ring takes beside the tile. */
	button.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) { .tile { min-height: var(--touch-target); } }
	.text { display: flex; flex-direction: column; flex: 1 1 auto; min-width: 0; }
	.title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-callout);
		font-weight: var(--fw-semibold);
		line-height: var(--lh-tight);
	}
	/* The title and its aside share one line, the aside on the title's baseline; only the title gives way. */
	.head { display: flex; align-items: baseline; gap: var(--space-2); min-width: 0; }
	.title.two-line {
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		white-space: normal;
		overflow-wrap: anywhere;
	}
	.when { flex: 0 0 auto; font-size: var(--text-footnote); line-height: var(--lh-tight); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
	.ctx {
		display: flex;
		align-items: center;
		gap: var(--space-1);
		min-width: 0;
		font-size: var(--text-footnote);
		line-height: var(--lh-tight);
		color: var(--text-secondary);
		white-space: nowrap;
	}
	/* Feedback counts never truncate mid-list: if the whole list does not fit, the segment wraps out of the one
	   visible line and is dropped, separator and all. The full counts stay in the accessible name. */
	.ctx.counted { flex-wrap: wrap; max-height: calc(var(--text-footnote) * var(--lh-tight)); overflow: hidden; }
	/* The state word and its dot, as the state line's snippet draws them: an open ask is "needs you", amber, as in
	   the sidebar, the plan and the attention dock. */
	.ctx :global(.state) { display: inline-flex; flex: 0 0 auto; align-items: center; gap: var(--space-1); }
	.ctx :global(.state.ask) { color: var(--amber-text); }
	.ctx :global(.dot) { flex: 0 0 auto; width: var(--space-2); height: var(--space-2); border-radius: var(--radius-circle); background: var(--amber); }
	/* A retired artifact keeps its name but is visibly out of reach: muted glyph, a dashed tile edge in the
	   stronger border colour so it holds in dark mode too, no chevron. */
	.tile.gone :global(.ic.filled) { background: transparent; outline: 1px dashed var(--text-tertiary); outline-offset: -1px; }
	.tile.gone .title, .tile.muted .title { color: var(--text-secondary); }
	.tile.gone .title, .tile.gone .ctx { color: var(--text-tertiary); }
	@media (prefers-reduced-motion: reduce) { .tile { transition: none; } }
</style>
