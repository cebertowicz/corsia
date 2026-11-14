<script lang="ts">
	import { decidedOpen } from '$lib/docCanvasComments';
	import type { DocComment } from '$lib/stores/docComments';
	/** Agree and Resolve for the one thread a phone sheet shows, in the sheet's header row beside Close; the card draws neither. */
	let { head, onAgree, onResolve }: { head: DocComment; onAgree: (id: string, on: boolean) => void; onResolve: (id: string) => void } = $props();
</script>

{#if !head.replacement && !head.resolved}<button type="button" class="sheet-act ack" class:on={head.acked} aria-pressed={head.acked} title="Agree" onclick={() => onAgree(head.id, !head.acked)}>Agree</button>{/if}
{#if (!head.replacement && !head.resolved) || decidedOpen(head)}<button type="button" class="sheet-act" title="Resolve — close the thread" onclick={() => onResolve(head.id)}>Resolve</button>{/if}

<style>
	.sheet-act { box-sizing: border-box; min-height: 44px; padding: 0 var(--space-2); border: 0; background: none; color: var(--accent-text); font: inherit; font-size: var(--text-subhead); font-weight: var(--fw-semibold); cursor: pointer; white-space: nowrap; border-radius: var(--radius-sm); }
	.sheet-act:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.sheet-act.ack.on { color: var(--green); text-decoration: underline; text-underline-offset: 3px; }
</style>
