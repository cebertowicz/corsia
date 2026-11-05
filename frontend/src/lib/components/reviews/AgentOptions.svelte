<script lang="ts" generics="T extends TaggableAgent">
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import { Button } from '$lib/components/ui';
	import { agentColor } from '$lib/docPresence';
	import type { TaggableAgent } from '$lib/api/docReviews';
	let { id, label, matches, active, choose, status = 'ready', retry }: { id: string; label: string; matches: T[]; active: number; choose: (reviewer: T) => void; status?: 'loading' | 'error' | 'empty' | 'ready'; retry?: () => Promise<void> } = $props();
</script>
<div {id} role="listbox" tabindex="-1" aria-label={label} class="options">
	{#each matches as reviewer, index (reviewer.id)}
		<!-- svelte-ignore a11y_click_events_have_key_events (The input owns keyboard navigation.) -->
		<div id={`${id}-option-${reviewer.id}`} role="option" tabindex="-1" aria-selected={index === active} class="option" class:active={index === active} onmousedown={(event) => event.preventDefault()} onclick={() => choose(reviewer)}>
			<Avatar name={reviewer.name} color={agentColor(reviewer.id)} hidden size="md" />
			<span class="option-text"><span class="option-name">{reviewer.name}</span>{#if reviewer.responsibility}<span class="option-detail" title={reviewer.responsibility}>{reviewer.responsibility}</span>{/if}</span>
		</div>
	{/each}
	{#if status === 'loading'}<p class="empty" role="status">Loading agents…</p>
	{:else if status === 'error'}<p class="empty" role="status">Couldn't load agents. <Button size="sm" variant="ghost" onclick={retry}>Retry</Button></p>
	{:else if status === 'empty'}<p class="empty" role="status">No agents in this project to tag.</p>
	{:else if !matches.length}<p class="empty" role="status">No matching agents.</p>{/if}
</div>
<style>
	.options { max-height: calc(var(--touch-target) * 4); overflow: auto; overscroll-behavior: contain; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-elevated); }
	.option { display: flex; align-items: center; gap: var(--space-2); min-height: calc(var(--space-7) + var(--space-1)); padding: var(--space-1) var(--space-2); cursor: pointer; }
	.option.active, .option:hover { background: var(--bg-hover); }
	.option-text { display: flex; flex-direction: column; min-width: 0; }
	.option-name { overflow-wrap: anywhere; }
	.option-detail { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.option-detail { color: var(--text-tertiary); font-size: var(--text-caption); }
	.empty { color: var(--text-secondary); font-size: var(--text-footnote); }
	.empty { margin: var(--space-2); }
	/* The Retry button is 28px tall (space-7 less space-1); its margins take it down to one text line, so the error row is as tall as the loading row and Retry's size and hit area are unchanged. */
	.empty :global(.btn) { vertical-align: middle; margin-block: calc((1lh - (var(--space-7) - var(--space-1))) / 2); }
	@media (max-width: 48rem) { .option { min-height: var(--touch-target); } }
</style>
