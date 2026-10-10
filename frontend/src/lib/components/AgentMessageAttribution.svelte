<script lang="ts">
	import { openSubagentRequest } from '$lib/stores/subagents';

	let { msg }: { msg: any } = $props();

	const fromThread = $derived((msg.metadata?.from_thread as string) || '');
	const fromTitle = $derived((msg.metadata?.from_title as string) || 'agent');

	function open() {
		if (fromThread) openSubagentRequest.set(fromThread);
	}
</script>

<button class="agent-attr" onclick={open} aria-label="Open sending thread: {fromTitle}">
	<span class="attr-glyph">▸</span>
	<span class="attr-prefix">From</span>
	<span class="attr-title">{fromTitle}</span>
</button>

<style>
	.agent-attr {
		display: inline-flex; align-items: center; gap: 5px;
		padding: 2px 8px 2px 6px;
		margin-bottom: 4px;
		border: none; border-radius: var(--radius-pill);
		background: var(--bg-tertiary); color: var(--text-tertiary);
		font-size: var(--text-caption); font-family: var(--font-mono);
		cursor: pointer;
		transition: background var(--dur-tight), color var(--dur-tight);
	}
	.agent-attr:hover { background: var(--bg-hover); color: var(--text-secondary); }
	.attr-glyph { flex-shrink: 0; font-size: var(--text-caption); color: var(--teal); }
	.attr-prefix { color: var(--text-tertiary); }
	.attr-title {
		max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
</style>
