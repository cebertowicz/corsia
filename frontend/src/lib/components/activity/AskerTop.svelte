<script lang="ts">
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';

	/** The top line of a decision, task or held message at a desk: who asks (a link to their chat) and what they wait for. */
	let { who, status, openChat }: { who: { name: string; id: string }; status: string; openChat: (threadId: string) => void } = $props();
</script>

<div class="asker-top">
	<button type="button" class="asker-top-link" aria-label={`Open chat with ${who.name}`} onclick={() => openChat(who.id)}><Avatar name={who.name} color={agentColor(who.id)} hidden /><span>{who.name}</span><span class="asker-top-colon" aria-hidden="true">:</span></button><span class="asker-top-status">{status}</span>
</div>

<style>
	.asker-top { display: flex; align-items: center; gap: var(--space-2); max-width: 40rem; margin: var(--space-3) auto 0; padding: 0 var(--space-5); font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	.asker-top-link { display: inline-flex; align-items: center; gap: var(--space-2); min-width: 0; padding: 0; border: 0; background: none; font: inherit; color: var(--accent-text); cursor: pointer; border-radius: var(--radius-xs); }
	.asker-top-link:hover span:not(.asker-top-colon) { text-decoration: underline; }
	.asker-top-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.asker-top-colon { margin-left: calc(-1 * var(--space-1)); }
</style>
