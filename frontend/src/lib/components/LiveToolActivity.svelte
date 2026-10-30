<script lang="ts">
	import {
		activeThreadId, activeToolsByThread, isThreadTurnActive, liveReasoningByThread, streamContentByThread,
	} from '$lib/stores/threads';
	import { turnStates } from '$lib/ws/turnState';
	import { renderMarkdown } from '$lib/markdown';
	import { holdHeight } from '$lib/actions/holdHeight';
	import ToolActivityBlock from './ToolActivityBlock.svelte';

	let {
		threadId = undefined,
		expandedTools,
		onToggleToolBlock,
	}: {
		/** The thread whose live turn this shows; defaults to the active thread. */
		threadId?: string | null;
		expandedTools: Set<string>;
		onToggleToolBlock: (id: string) => void;
	} = $props();

	/** Collapsing a tool step is the person's own change of height; the slot below lets go of its floor for it. */
	let collapses = $state(0);
	function toggleTool(id: string): void {
		collapses++;
		onToggleToolBlock(id);
	}

	const boundThreadId = $derived(threadId ?? $activeThreadId);
	const activeTools = $derived((boundThreadId && $activeToolsByThread.get(boundThreadId)) || []);
	const liveReasoning = $derived((boundThreadId && $liveReasoningByThread.get(boundThreadId)) || '');
	const streamContent = $derived((boundThreadId && $streamContentByThread.get(boundThreadId)) || '');
	const isStreaming = $derived(isThreadTurnActive($turnStates, boundThreadId));

	function autoScroll(node: HTMLElement, _content: string) {
		node.scrollTop = node.scrollHeight;
		return {
			update() { node.scrollTop = node.scrollHeight; }
		};
	}

	const liveTools = $derived(activeTools.map((t) => ({
		id: 'live-' + t.id,
		name: t.name,
		hint: t.hint,
		args: t.args,
		result: t.result,
		phrase: t.phrase,
		object: t.object,
		outcome: t.outcome,
		errorLine: t.error_line,
		status: t.status,
	})));

	// The live region belongs to a live turn, and a spinner must never outlive
	// one. Tool buffers are cleared on every terminal, so this changes nothing in
	// the ordinary case — it makes "the turn ended" sufficient to stop spinning,
	// rather than depending on the clear having reached us.
	const showLiveTools = $derived(isStreaming && activeTools.length > 0);

	// #1083: once every known tool reaches 'done', the turn keeps generating
	// (its next call_llm) with no interstitial frame — the finished tool set
	// alone must not read as "nothing is happening". Reuse the same truthful
	// gate the non-tool branch below uses: only silence when there is
	// genuinely nothing live to show yet.
	const anyToolRunning = $derived(activeTools.some((t) => t.status === 'running'));
	const showQuietGapIndicator = $derived(
		!anyToolRunning && !liveReasoning && !streamContent
	);
</script>

{#if showLiveTools || isStreaming}
<!-- Between steps this slot swaps reasoning, tools and the quiet-gap dots; held at its tallest, it never shortens the transcript under a reader at the live edge. -->
<div class="live-slot" use:holdHeight={`${boundThreadId}:${collapses}`}>
{#if showLiveTools}
	<div class="message assistant tool-activity">
		<div class="assistant-msg">
			{#if liveReasoning}
				<div class="live-reasoning compact">
					<div class="live-reasoning-header">
						<svg class="reasoning-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>
						</svg>
						<span>Thinking...</span>
					</div>
					<div class="live-reasoning-text" use:autoScroll={liveReasoning}><div class="md-content">{@html renderMarkdown(liveReasoning)}</div></div>
				</div>
			{/if}
			<div class="tool-calls">
				<ToolActivityBlock
					tools={liveTools}
					blockKey="live-block"
					live
					expandedTools={expandedTools}
					onToggle={toggleTool}
				/>
			</div>
			{#if showQuietGapIndicator}
				<div class="thinking-indicator">
					<span class="dot"></span><span class="dot"></span><span class="dot"></span>
				</div>
			{/if}
		</div>
	</div>
{:else if isStreaming}
	<div class="message assistant">
		<div class="assistant-msg">
			{#if liveReasoning}
				<div class="live-reasoning" class:compact={streamContent}>
					<div class="live-reasoning-header">
						<svg class="reasoning-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
							<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>
						</svg>
						<span>Thinking...</span>
					</div>
					<div class="live-reasoning-text" use:autoScroll={liveReasoning}><div class="md-content">{@html renderMarkdown(liveReasoning)}</div></div>
				</div>
			{/if}
			{#if streamContent}
				<div class="md-content">{@html renderMarkdown(streamContent)}</div>
			{:else if !liveReasoning}
				<div class="thinking-indicator">
					<span class="dot"></span><span class="dot"></span><span class="dot"></span>
				</div>
			{/if}
		</div>
	</div>
{/if}
</div>
{/if}

<style>
	.live-slot { display: flow-root; }
	.message { max-width: 760px; margin: 0 auto 20px; overflow-x: hidden; }
	.message.assistant { animation: fadeInUp var(--dur-spring) ease; }
	@keyframes fadeInUp {
		from { opacity: 0; transform: translateY(8px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.tool-calls {
		display: flex; flex-direction: column; gap: 4px;
		padding-left: 8px; border-left: 2px solid color-mix(in srgb, var(--teal) 35%, transparent);
		border-radius: 1px;
	}

	.thinking-indicator { display: flex; gap: 4px; padding: 8px 0; }
	.thinking-indicator .dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle);
		background: var(--text-tertiary); animation: pulse 1.4s infinite;
	}
	.thinking-indicator .dot:nth-child(2) { animation-delay: 200ms; }
	.thinking-indicator .dot:nth-child(3) { animation-delay: 400ms; }
	@keyframes pulse { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

	.live-reasoning { margin-bottom: 4px; }
	.live-reasoning.compact { margin-bottom: 8px; }
	.live-reasoning.compact .live-reasoning-text { max-height: 80px; }
	.live-reasoning-header {
		display: flex; align-items: center; gap: 6px;
		font-size: var(--text-footnote); color: var(--text-secondary); margin-bottom: 4px;
	}
	.live-reasoning-header .reasoning-icon { animation: pulse 1.4s infinite; }
	.live-reasoning-text {
		background: var(--bg-secondary); border-left: 2px solid color-mix(in srgb, var(--accent) 40%, transparent);
		padding: 6px 10px; border-radius: var(--radius-2xs);
		max-height: 150px; overflow-y: auto;
	}

	@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>
