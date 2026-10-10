<script lang="ts">
	import { token } from '$lib/stores/auth';
	import {
		hydrateSubagentThreadOnce,
		openSubagentRequest,
		registerSubagent,
		subagentMeta,
	} from '$lib/stores/subagents';
	import { subagentGlyph } from '$lib/utils/subagentLabel';

	let { msg }: { msg: any } = $props();

	const childId = $derived((msg.metadata?.child_thread_id as string) || '');
	const threadKind = $derived((msg.metadata?.thread_kind as string) || 'subagent');
	const name = $derived((msg.metadata?.name as string) || kindName(threadKind).toLowerCase());
	const provider = $derived((msg.metadata?.provider as string) || 'cc');
	const parentId = $derived(
		(msg.metadata?.parent_thread_id as string) ||
		(msg.parent_thread_id as string) ||
		(msg.thread_id as string) ||
		''
	);
	// The marker message is durable; the live map is not. Absent live metadata
	// the chip knows NOTHING about the worker, and says so — a chip that reads
	// "open" makes a worker that errored, one that finished, and one running
	// right now render identically.
	const status = $derived($subagentMeta.get(childId)?.status ?? 'unknown');
	const running = $derived(status === 'running');
	const isSubagent = $derived(threadKind === 'subagent');

	// `idle` is a server-confirmed "not running"; it deliberately does not claim
	// how the worker ended, because runtime truth cannot tell us that.
	const LABEL: Record<string, string> = {
		running: 'running', ready: 'ready', closed: 'done', error: 'error',
		idle: 'idle', unknown: 'unknown',
	};

	// Re-read what the server knows so a reload does not erase it. Chips render
	// per spawn marker, so this asks once per thread per page load. A peer
	// thread has no worker status to read — its slot is a link.
	$effect(() => {
		if (childId && isSubagent && $token) hydrateSubagentThreadOnce(childId, $token, parentId);
	});

	function kindName(value: string): string {
		if (value === 'subagent') return 'Worker';
		return 'Thread';
	}

	function directionLabel(value: string): string {
		return `Spawned ${kindName(value).toLowerCase()}`;
	}

	// A spawned PEER thread is a link, not a worker under supervision: its slot
	// is the affordance "open this thread", not a claim about an outcome.
	function statusLabel(value: string): string {
		if (!isSubagent) return 'open';
		return LABEL[value] ?? value;
	}

	function open() {
		if (!childId) return;
		// Only a WORKER belongs in the subagent map: membership is what routes a
		// send to a child's stdin channel, so registering a spawned peer thread
		// would point its sends at a channel that does not exist.
		if (isSubagent && parentId && !$subagentMeta.has(childId)) {
			// Restores linkage only. Clicking a chip teaches us nothing about what
			// the worker did, so it asserts nothing about it.
			registerSubagent(parentId, childId, name, provider, 'unknown');
		}
		openSubagentRequest.set(childId);
	}
</script>

<button class="sa-chip" class:running onclick={open}>
	{#if isSubagent && (status === 'ready' || status === 'closed')}
		<span class="sa-check" class:done={status === 'closed'}>✓</span>
	{:else}
		<span class="sa-dot" class:run={running} class:err={status === 'error'} class:unk={status === 'unknown'}></span>
	{/if}
	<span class="sa-glyph">{subagentGlyph(provider)}</span>
	<span class="sa-action">{directionLabel(threadKind)}</span>
	<span class="sa-name">{name}</span>
	<span class="sa-status">{statusLabel(status)}</span>
	<span class="sa-chev">›</span>
</button>

<style>
	.sa-chip {
		display: flex; align-items: center; gap: 8px; width: 100%; max-width: 100%;
		text-align: left; cursor: pointer; padding: 7px 11px;
		border: none; border-left: 2px solid var(--border);
		background: none; border-radius: 0 8px 8px 0; color: var(--text-secondary);
		transition: background var(--dur-tight), border-color var(--dur-tight);
	}
	.sa-chip:hover { background: var(--bg-hover); }
	.sa-chip.running {
		border-left-color: var(--teal);
		background: color-mix(in srgb, var(--teal) 6%, transparent);
	}
	.sa-dot { flex-shrink: 0; width: 7px; height: 7px; border-radius: var(--radius-circle); background: var(--text-tertiary); }
	.sa-dot.run { background: var(--teal); animation: pulse 1.6s ease-in-out infinite; }
	.sa-dot.err { background: var(--red); }
	/* Hollow, because there is nothing behind it: no state is being claimed. */
	.sa-dot.unk { background: none; box-shadow: inset 0 0 0 1px var(--text-tertiary); }
	.sa-check { flex-shrink: 0; font-size: var(--text-footnote); color: var(--green); }
	.sa-check.done { color: var(--text-tertiary); }
	.sa-glyph { flex-shrink: 0; font-size: var(--text-footnote); color: var(--teal); }
	.sa-action {
		flex-shrink: 0;
		font-size: var(--text-caption);
		font-family: var(--font-mono);
		color: var(--text-tertiary);
	}
	.sa-name { flex: 1; min-width: 0; font-size: var(--text-footnote); font-family: var(--font-mono); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.sa-status { flex-shrink: 0; font-size: var(--text-caption); font-family: var(--font-mono); color: var(--text-tertiary); }
	.sa-chev { flex-shrink: 0; font-size: var(--text-callout); color: var(--text-tertiary); }
	@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

	@media (max-width: 768px) {
		.sa-chip { padding: 12px 11px; }
	}
</style>
