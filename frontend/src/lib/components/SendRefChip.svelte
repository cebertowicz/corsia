<script lang="ts">
	import { openSubagentRequest } from '$lib/stores/subagents';

	let { msg }: { msg: any } = $props();

	const targetId = $derived((msg.metadata?.target_thread_id as string) || '');
	const targetTitle = $derived((msg.metadata?.target_title as string) || 'thread');
	const targetKind = $derived((msg.metadata?.target_kind as string) || 'thread');

	function kindName(value: string): string {
		if (value === 'subagent') return 'Worker';
		return 'Thread';
	}

	function open() {
		if (targetId) openSubagentRequest.set(targetId);
	}
</script>

<button class="send-chip" onclick={open} aria-label="Open sent-to thread: {targetTitle}">
	<span class="send-label">Sent to</span>
	<span class="send-kind">{kindName(targetKind)}</span>
	<span class="send-title">{targetTitle}</span>
	<span class="send-chev">›</span>
</button>

<style>
	.send-chip {
		display: flex; align-items: center; gap: var(--space-2);
		width: 100%; max-width: 100%;
		padding: var(--space-1) var(--space-3);
		border: none; border-left: 2px solid var(--border-subtle);
		border-radius: 0 var(--radius-xs) var(--radius-xs) 0;
		background: none;
		color: var(--text-tertiary);
		text-align: left;
		cursor: pointer;
		transition: background var(--dur-tight), border-color var(--dur-tight);
	}
	.send-chip:hover {
		background: var(--bg-hover);
		border-left-color: var(--teal);
	}
	.send-label,
	.send-kind,
	.send-title {
		font-family: inherit;
		font-size: var(--text-footnote);
	}
	.send-label {
		flex-shrink: 0;
		padding: 1px 6px;
		border-radius: var(--radius-xs);
		border: 1px solid var(--border-subtle);
		background: var(--bg-secondary);
		color: var(--text-secondary);
		font-size: var(--text-caption);
	}
	.send-kind {
		flex-shrink: 0;
		color: var(--text-tertiary);
		font-size: var(--text-caption);
	}
	.send-title {
		flex: 1; min-width: 0;
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
		color: var(--text-secondary);
	}
	.send-chev {
		flex-shrink: 0;
		color: var(--text-tertiary);
		font-size: var(--text-callout);
	}

	@media (max-width: 768px) {
		.send-chip { padding: 12px 11px; }
	}
</style>
