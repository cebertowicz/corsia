<script lang="ts">
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { agentColor } from '$lib/docPresence';

	/** The top line of a decision, task or held message at a desk: who asks (a link to their chat when they have one) and what they wait for. */
	let { who, status, openChat }: { who: { name: string; id: string }; status: string; openChat: (threadId: string) => void } = $props();
</script>

<div class="asker-top">
	<Avatar name={who.name} color={agentColor(who.id || who.name)} hidden />
	{#if who.id}
		<button type="button" class="asker-top-link" aria-label={`Open chat with ${who.name}`} onclick={() => openChat(who.id)}><span>{who.name}:</span></button>
	{:else}
		<span class="asker-top-link plain"><span>{who.name}:</span></span>
	{/if}<span class="asker-top-status">{status}</span>
</div>

<style>
	.asker-top { display: flex; align-items: center; gap: var(--space-2); margin: 0 0 var(--space-3); font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	.asker-top-link { display: inline-flex; align-items: center; min-width: 0; padding: 0; border: 0; background: none; font: inherit; color: var(--accent-text); cursor: pointer; border-radius: var(--radius-xs); }
	.asker-top-link:hover span { text-decoration: underline; }
	.asker-top-link.plain { color: var(--text-primary); cursor: default; }
	.asker-top-link.plain:hover span { text-decoration: none; }
	.asker-top-link:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
