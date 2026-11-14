<script lang="ts" generics="T extends TaggableAgent & { paused?: boolean }">
	import Avatar from '$lib/components/ui/Avatar.svelte';
	import MiddleName from '$lib/components/MiddleName.svelte';
	import { Button } from '$lib/components/ui';
	import { agentColor } from '$lib/docPresence';
	import type { TaggableAgent } from '$lib/api/docReviews';
	let { id, label, matches, active, choose, status = 'ready', retry, compact = false }: { id: string; label: string; matches: T[]; active: number; choose: (reviewer: T) => void; status?: 'loading' | 'error' | 'empty' | 'no_thread' | 'ready'; retry?: () => Promise<void>; compact?: boolean } = $props();
	const detail = (reviewer: T) => [reviewer.paused ? 'Paused' : '', reviewer.responsibility].filter(Boolean).join(' · ');
</script>
<div {id} role="listbox" tabindex="-1" aria-label={label} class="options" class:compact>
	{#each matches as reviewer, index (reviewer.id)}
		<!-- svelte-ignore a11y_click_events_have_key_events (The input owns keyboard navigation.) -->
		<div id={`${id}-option-${reviewer.id}`} role="option" tabindex="-1" aria-label={detail(reviewer) ? `${reviewer.name} ${detail(reviewer)}` : reviewer.name} aria-selected={index === active} class="option" class:active={index === active} onmousedown={(event) => event.preventDefault()} onclick={() => choose(reviewer)}>
			<Avatar name={reviewer.name} color={agentColor(reviewer.id)} hidden size="md" />
			{#if compact}
				<!-- The responsibility starts with a zero-width space, so when not even its first word fits that word wraps onto the hidden second line and the responsibility drops whole. -->
				<span class="option-text"><span class="option-name"><MiddleName name={reviewer.name} /></span>{#if detail(reviewer)}<span class="option-detail" class:paused={reviewer.paused} title={detail(reviewer)}>{#if reviewer.paused}<span class="option-paused">Paused</span>{/if}{#if reviewer.responsibility}<span class="option-role">&#8203;{reviewer.paused ? ' · ' : ''}{reviewer.responsibility}</span>{/if}</span>{/if}</span>
			{:else}
				<span class="option-text"><span class="option-name" title={reviewer.name}>{reviewer.name}</span>{#if reviewer.responsibility}<span class="option-detail" title={reviewer.responsibility}>{reviewer.responsibility}</span>{/if}</span>
			{/if}
		</div>
	{/each}
	{#if status === 'loading'}<p class="empty" role="status">Loading agents…</p>
	{:else if status === 'error'}<p class="empty" role="status">Couldn’t load agents. <Button size="sm" variant="ghost" onclick={retry}>Retry</Button></p>
	{:else if status === 'no_thread'}<p class="empty" role="status">Open this document from a chat to tag agents.</p>
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
	.compact .option-text { flex: 1; flex-direction: row; align-items: center; gap: var(--space-2); }
	/* The name keeps its full width and the responsibility gives way first, so two agents with the same name still read
	   whole. A long name shrinks (MiddleName cuts it) only down to where "Paused" still fits whole. */
	.compact .option-name { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	/* A grid whose responsibility track has a floor of 0, so the detail's own floor is "Paused" alone (or nothing). */
	.compact .option-detail { flex: 1 1 0; min-width: min-content; display: grid; grid-template-columns: max-content minmax(0, max-content); justify-content: end; white-space: normal; text-overflow: clip; }
	.compact .option-paused { grid-column: 1; white-space: nowrap; }
	/* The responsibility wraps between words and shows only its first line, so every cut falls between words. */
	.compact .option-role { grid-column: 2; max-height: 1lh; overflow: hidden; overflow-wrap: normal; word-break: normal; text-align: end; }
	.compact .paused .option-role { text-align: start; }
	.empty { color: var(--text-secondary); font-size: var(--text-footnote); }
	.empty { margin: var(--space-2); }
	/* The Retry button is 28px tall (space-7 less space-1); its margins take it down to one text line, so the error row is as tall as the loading row and Retry's size and hit area are unchanged. */
	.empty :global(.btn) { vertical-align: middle; margin-block: calc((1lh - (var(--space-7) - var(--space-1))) / 2); }
	@media (max-width: 48rem), (any-pointer: coarse) { .option { min-height: var(--touch-target); } }
</style>
