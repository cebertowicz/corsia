<script lang="ts">
	import AskerLink from './AskerLink.svelte';

	/** The docked card's rows when the asker's name links to their chat: row 1 the name, row 2 the ask with its chevron; a status with nothing more to open shares the name's line. */
	let { who, plus = 0, ask, full, lineLabel, expandable, cardOpen = $bindable(), openChat }: {
		who: { name: string; id: string };
		plus?: number;
		ask: string;
		full: string;
		lineLabel: string;
		expandable: boolean;
		cardOpen: boolean;
		openChat: (threadId: string) => void;
	} = $props();
</script>

{#if expandable}
	<div class="rb-dock-by-row"><AskerLink {who} {plus} cut={!cardOpen} colon={false} {openChat} /></div>
	<button type="button" class="rb-dock-ask rb-dock-line" title={full || ask} aria-expanded={cardOpen} aria-label={lineLabel || `${who.name}${plus ? ` +${plus}` : ''}: ${full || ask}`} onclick={() => (cardOpen = !cardOpen)}>
		<span class="rb-dock-q">{cardOpen ? (full || ask) : ask}</span>
		<svg class="rb-icon rb-dock-chev" class:down={cardOpen} aria-hidden="true" viewBox="0 0 16 16"><path d="M4 6.5l4 4 4-4" /></svg>
	</button>
{:else}
	<div class="rb-dock-statusline"><AskerLink {who} {plus} cut colon {openChat} /><span class="rb-dock-q fixed">{full || ask}</span></div>
{/if}

<style>
	/* Docked (phone): row 1 the name, row 2 the ask with its chevron, then the answers. A status with nothing more to open shares the name's line. */
	.rb-dock-by-row { flex: 1 0 100%; display: flex; align-items: center; min-height: var(--touch-target); min-width: 0; font-size: var(--text-subhead); line-height: var(--lh-normal); }
	:global(.rb-dock.linked.open) .rb-dock-by-row { padding-block: var(--space-2); }
	.rb-dock-line { display: flex; align-items: center; gap: var(--space-3); min-height: var(--touch-target); }
	.rb-dock-line::after { display: none; }
	.rb-dock-line .rb-dock-q { flex: 1 1 0; min-width: 0; margin: 0; color: var(--text-primary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	:global(.rb-dock.linked:not(.open)) .rb-dock-line .rb-dock-q { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.rb-dock-chev { flex: none; color: var(--text-tertiary); transform: rotate(180deg); }
	.rb-dock-chev.down { transform: none; }
	.rb-dock-statusline { flex: 1 0 100%; display: flex; flex-wrap: wrap; align-items: center; column-gap: var(--space-2); min-height: var(--touch-target); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.rb-dock-statusline .rb-dock-q { flex: 0 0 auto; min-width: 0; color: var(--text-secondary); }
</style>
