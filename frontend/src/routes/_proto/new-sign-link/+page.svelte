<script lang="ts">
	import { onMount } from 'svelte';
	import '$lib/components/commentAnchors.css';
	// PROTOTYPE (uncommitted): an unread highlight next to a real link, to choose a treatment that is not mistaken for one.
	const params = new URLSearchParams(location.search);
	document.documentElement.setAttribute('data-theme', params.get('theme') ?? 'light');
	const treat = params.get('treat') ?? 'A';
	document.documentElement.setAttribute('data-treat', treat);
	onMount(() => { document.documentElement.dataset.ready = '1'; });
</script>

<main class="page">
	<h2>Treatment {treat}</h2>
	<p class="md-content chat">Chat: the <a href="/rollout">rollout guide</a> says to <span class="cm-comment-anchor" data-unread>confirm the renewal date</span> before Friday, and the <span class="cm-comment-anchor">rollback owner</span> has already read it.</p>
	<p class="doc">Document: the <span class="lnk">rollout guide</span> says to <span class="cm-comment-anchor" data-unread>confirm the renewal date</span> before Friday, and the <span class="cm-comment-anchor">rollback owner</span> has already read it.</p>
	<p class="doc">A link inside an unread highlight: <span class="cm-comment-anchor" data-unread>see the <span class="lnk">rollout guide</span> first</span>.</p>
	<p class="doc">One word: <span class="cm-comment-anchor" data-unread>Cut</span> the time, and a link <span class="lnk">guide</span>.</p>
</main>

<style>
	.page { padding: 20px 16px; max-width: 560px; background: var(--bg-primary); color: var(--text-primary); min-height: 100vh; font-size: var(--text-body, 16px); line-height: 1.6; }
	h2 { font-size: 13px; margin: 0 0 12px; color: var(--text-secondary); }
	.doc .lnk { color: var(--accent); text-decoration: underline; text-decoration-color: var(--accent-line); }
	.chat :global(a) { color: var(--accent-text); text-decoration: none; }
	:global(html[data-treat='E'] .cm-comment-anchor[data-unread]) { background-color: color-mix(in srgb, var(--accent) 22%, transparent); border-bottom: medium solid var(--text-primary); }
	:global(html[data-treat='F'] .cm-comment-anchor[data-unread]) { background-color: color-mix(in srgb, var(--accent) 22%, transparent); border-bottom: medium solid transparent; }
	:global(html[data-treat='H'] .cm-comment-anchor[data-unread]) { background-color: color-mix(in srgb, var(--accent) 18%, transparent); border-bottom: medium solid transparent; box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 60%, transparent); border-radius: var(--radius-2xs); }
	:global(html[data-treat='I'] .cm-comment-anchor[data-unread]) { background-color: color-mix(in srgb, var(--accent) 22%, transparent); border-bottom: medium solid var(--text-primary); border-bottom-style: dotted; }
</style>
