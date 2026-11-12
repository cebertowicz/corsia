<script lang="ts">
	import { onMount } from 'svelte';
	import '$lib/components/commentAnchors.css';
	// PROTOTYPE: unread highlights beside a real link. `?rows=1` stacks the candidate underlines for the light-mode weight decision; `?fc=1` adds forced-colour rows.
	const params = new URLSearchParams(location.search);
	document.documentElement.setAttribute('data-theme', params.get('theme') ?? 'light');
	const rows: { id: string; label: string }[] = [
		{ id: 'cur', label: 'Current: wash and 3 px solid ink underline' },
		{ id: 'L1', label: 'Option 1: wash and 2 px solid ink underline' },
		{ id: 'L2', label: 'Option 2: wash and 2 px dashed ink underline' },
		{ id: 'L3', label: 'Option 3: wash and 2 px solid ink underline at 55%' },
	];
	const fc = params.get('fc') === '1';
	const fcRows: { id: string; label: string }[] = [
		{ id: 'fc-cur', label: 'Forced colours, current: double rule and outline' },
		{ id: 'fc-mark', label: 'Forced colours, option: system marked-text colours' },
	];
	onMount(() => { document.documentElement.dataset.ready = '1'; });
</script>

<main class="page">
	{#each fc ? fcRows : rows as row (row.id)}
		<section data-row={row.id}>
			<h2>{row.label}</h2>
			<p class="md-content chat">Chat: the <a href="/rollout">rollout guide</a> says to <span class="cm-comment-anchor" data-unread>confirm the renewal date</span> before Friday, and the <span class="cm-comment-anchor">rollback owner</span> has already read it.</p>
			<p class="doc">Document: the <span class="lnk">rollout guide</span> says to <span class="cm-comment-anchor" data-unread>confirm the renewal date</span> before Friday, and the <span class="cm-comment-anchor">rollback owner</span> has already read it.</p>
		</section>
	{/each}
</main>

<style>
	.page { padding: 16px; max-width: 720px; background: var(--bg-primary); color: var(--text-primary); min-height: 100vh; font-size: var(--text-body, 16px); line-height: 1.6; }
	section { margin: 0 0 18px; }
	h2 { font-size: 13px; margin: 0 0 4px; color: var(--text-secondary); font-weight: 600; }
	p { margin: 0 0 6px; }
	.doc .lnk { color: var(--accent); text-decoration: underline; text-decoration-color: var(--accent-line); }
	.chat :global(a) { color: var(--accent-text); text-decoration: none; }
	:global([data-row='L1'] .cm-comment-anchor[data-unread]) { border-bottom-width: 2px; }
	:global([data-row='L2'] .cm-comment-anchor[data-unread]) { border-bottom-width: 2px; border-bottom-style: dashed; }
	:global([data-row='L3'] .cm-comment-anchor[data-unread]) { border-bottom-width: 2px; border-bottom-color: color-mix(in srgb, var(--text-primary) 55%, transparent); }
	@media (forced-colors: active) {
		:global([data-row='fc-mark'] .cm-comment-anchor[data-unread]) { forced-color-adjust: none; background: Mark; color: MarkText; border-bottom: 2px solid MarkText; outline: none; }
	}
</style>
