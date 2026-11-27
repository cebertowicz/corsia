<script lang="ts">
	import MiddleName from './MiddleName.svelte';

	/** The asker's name as a link to their chat (Activity): the .ct-link look at the footnote size of the line it sits in. An empty id means no chat to open: the name shows plain. */
	let { who, plus = 0, cut, colon, openChat }: {
		who: { name: string; id: string };
		plus?: number;
		/** Shorten a long name in the middle; false lets it wrap whole. */
		cut: boolean;
		colon: boolean;
		openChat: (threadId: string) => void;
	} = $props();
</script>

{#snippet label()}<strong>{#if cut}<MiddleName name={who.name} />{:else}<span class="asker-whole">{who.name}</span>{/if}</strong>{#if plus}<span class="rb-plus" aria-hidden="true">+{plus}</span>{/if}{#if colon}<span class="asker-colon">:</span>{/if}{/snippet}
{#if who.id}
	<button type="button" class="asker-link" aria-label={`Open chat with ${who.name}`} onclick={() => openChat(who.id)}>{@render label()}</button>
{:else}
	<!-- An asker with no chat of their own: the name, not a link that goes nowhere. -->
	<span class="asker-link plain">{@render label()}</span>
{/if}

<style>
	.asker-link { --ct-hit: var(--space-6); position: relative; flex: 0 1 auto; min-width: 0; max-width: 100%; display: inline-flex; align-items: center; padding: 0; border: 0; background: none; font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); color: var(--accent-text); text-align: left; cursor: pointer; }
	.asker-link strong { font-weight: inherit; min-width: 0; display: inline-flex; }
	@media (pointer: coarse) { .asker-link { --ct-hit: var(--touch-target); } }
	.asker-link::after { content: ''; position: absolute; inset: min(0px, calc(50% - var(--ct-hit) / 2)); }
	.asker-link:hover strong { text-decoration: underline; }
	.asker-link.plain { color: var(--text-primary); cursor: default; }
	.asker-link.plain:hover strong { text-decoration: none; }
	.asker-link.plain::after { display: none; }
	.asker-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.asker-colon { flex: none; }
	.rb-plus { margin-left: var(--space-1); color: var(--text-secondary); font-weight: var(--fw-semibold); }
	:global(.rb-dock-by-row) .asker-link, :global(.rb-dock-statusline) .asker-link { min-height: var(--touch-target); }
	:global(.rb-dock.open) .asker-whole { display: block; white-space: normal; overflow-wrap: anywhere; }
	:global(.rb-line-linked) .asker-link { pointer-events: auto; }
</style>
