<script lang="ts">
	import { renderMarkdown } from '$lib/markdown';
	import { formatTime } from '$lib/time';
	import { externalModelDisplayName } from '$lib/modelDisplayName';
	import { models, type Model } from '$lib/stores/models';
	import { canonicalName } from '$lib/stores/modelGroups';
	import { parseBrowserChip } from '$lib/browserChip';
	import MessageArtifactCards from './MessageArtifactCards.svelte';
	import ToolActivityBlock from './ToolActivityBlock.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';

	let {
		msg,
		expandedTools,
		expandedReasoning,
		onToggleToolBlock,
		onToggleReasoning,
		onLoadToolResult,
		showActivity = true,
		showContent = true,
		hiddenHolds,
		renderedAsks,
	}: {
		msg: any;
		expandedTools: Set<string>;
		expandedReasoning: Set<string>;
		onToggleToolBlock: (id: string) => void;
		onToggleReasoning: (msgId: string) => void;
		onLoadToolResult?: (toolCallId: string) => void;
		showActivity?: boolean;
		showContent?: boolean;
		/** Holds whose ask card renders on their blocked update row instead. */
		hiddenHolds?: ReadonlySet<string>;
		/** Holds whose ask card is on screen elsewhere in the transcript. */
		renderedAsks?: ReadonlySet<string>;
	} = $props();

	let openingAnnouncement = $state('');
	$effect(() => {
		// Populate a stable live region once. Reveal ticks and the final markdown
		// swap are visual only, and a later mount of history does not announce.
		if (msg.firstRunOpeningReveal && msg.typewriterPos != null && !openingAnnouncement) {
			openingAnnouncement = msg.content;
		}
	});

	const activityTools = $derived.by(() => {
		const hints: string[] = msg.toolHints ?? [];
		const calls: any[] = msg.metadata?.tool_calls ?? [];
		const n = Math.max(hints.length, calls.length);
		return Array.from({ length: n }, (_, j) => ({
			id: msg.id + '-tool-' + j,
			name: calls[j]?.name ?? (hints[j] ?? '').split('(')[0].trim(),
			hint: hints[j],
			args: calls[j]?.args,
			result: calls[j]?.result,
			phrase: calls[j]?.phrase,
			object: calls[j]?.object,
			outcome: calls[j]?.outcome,
			errorLine: calls[j]?.error_line,
			resultState: calls[j]?.result_state,
			resultError: calls[j]?.result_error,
			callId: calls[j]?.id,
			status: 'done' as const,
		}));
	});
	// The Step that opened the browser carries the chip the server stored on it as the open settled.
	const browserChip = $derived(parseBrowserChip(msg.metadata?.browser_chip));
	const footerLabel = $derived(messageFooterLabel(msg.metadata, $models));

	function modelTail(id: string): string {
		if (id.includes('/')) return id.split('/').pop() ?? id;
		if (id.includes('@')) return id.split('@').pop() ?? id;
		return id;
	}

	function titleWords(value: string): string {
		return value
			.replace(/-/g, ' ')
			.replace(/\b[a-z]/g, (char) => char.toUpperCase());
	}

	function formatExternalModelName(id: string): string {
		return externalModelDisplayName(modelTail(id));
	}

	function harnessLabel(metadata: any): string | null {
		const harness = metadata?.harness;
		const model = String(metadata?.model ?? metadata?.model_name ?? '');
		if (harness === 'claude-code' || metadata?.cc) return 'Claude Code';
		if (harness === 'codex' || metadata?.codex) return 'Codex';
		if (harness === 'native' && model.startsWith('omlx@')) return 'oMLX';
		if (harness === 'omlx' || model.startsWith('omlx@')) return 'oMLX';
		if (harness === 'native') return 'Native';
		if (harness) return titleWords(String(harness));
		if (model.startsWith('claude-code/')) return 'Claude Code';
		if (model.startsWith('codex/')) return 'Codex';
		return null;
	}

	function modelLabel(metadata: any, catalog: Model[]): string | null {
		const model = String(metadata?.model ?? '');
		const modelName = metadata?.model_name ? String(metadata.model_name) : '';
		if (modelName && !modelName.includes('/') && !modelName.includes('@')) return modelName;
		if (!model && !modelName) return null;
		const raw = model || modelName;
		if (raw.startsWith('claude-code/') || raw.startsWith('codex/')) return formatExternalModelName(raw);
		if (raw.startsWith('omlx@')) {
			const stored = catalog.find((entry) => entry.litellm_model === raw || entry.id === raw);
			return stored ? canonicalName(stored) : modelTail(raw);
		}
		return modelTail(raw);
	}

	function messageFooterLabel(metadata: any, catalog: Model[]): string | null {
		const harness = harnessLabel(metadata);
		const model = modelLabel(metadata, catalog);
		// Native history displays its stored catalog identity, falling back to its model tail.
		if (String(metadata?.model ?? metadata?.model_name ?? '').startsWith('omlx@') && model) return model;
		if (harness && model) return `${harness} · ${model}`;
		return model ?? harness;
	}

</script>

<div class="assistant-msg">
	{#if showActivity && activityTools.length > 0}
		<div class="tool-calls">
			<ToolActivityBlock
				tools={activityTools}
				blockKey={msg.id + '-tools'}
				expandedTools={expandedTools}
				onToggle={onToggleToolBlock}
					onLoadResult={onLoadToolResult
						? (tool) => {
							if (tool.callId) onLoadToolResult(tool.callId);
						}
						: undefined}
			/>
		</div>
	{/if}

	{#if showActivity && msg.reasoning}
		<button class="reasoning-toggle" onclick={() => onToggleReasoning(msg.id)}>
			<svg class="reasoning-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
				<path d="M9 4a3 3 0 00-3 3 3 3 0 00-3 3 3 3 0 001 5 3 3 0 005 1 3 3 0 003-3V4z"/><path d="M15 4a3 3 0 013 3 3 3 0 013 3 3 3 0 01-1 5 3 3 0 01-5 1 3 3 0 01-3-3V4z"/>
			</svg>
			<span>Thought</span>
			<svg class="reasoning-chevron" class:expanded={expandedReasoning.has(msg.id)} width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>
		</button>
		{#if expandedReasoning.has(msg.id)}
			<div class="reasoning-content"><div class="md-content">{@html renderMarkdown(msg.reasoning)}</div></div>
		{/if}
	{/if}

	{#if showContent && msg.content}
	<div class="md-content long-message-body">
		{#if msg.firstRunOpeningReveal}
			<span class="sr-only" aria-live="polite" aria-atomic="true">{openingAnnouncement}</span>
		{/if}
		<div aria-hidden={openingAnnouncement || (msg.firstRunOpeningReveal && msg.typewriterPos != null) ? 'true' : undefined}>
			{#if msg.typewriterPos != null}
				<pre class="typewriter-text">{msg.content.slice(0, msg.typewriterPos)}</pre>
				<span class="typewriter-cursor" aria-hidden="true">&#9613;</span>
			{:else}
				{@html renderMarkdown(msg.content)}
			{/if}
		</div>
	</div>
	{/if}

	<ChatCommentThreads messageId={msg.id} threadId={msg.thread_id} comments={msg.chat_comments ?? []} />

	{#if msg.cards?.length || browserChip}
		<MessageArtifactCards cards={msg.cards ?? []} browser={browserChip} threadId={msg.thread_id} {hiddenHolds} {renderedAsks} />
	{/if}

		<div class="msg-footer">
			<span class="msg-time">{formatTime(msg.created_at)}</span>
			{#if footerLabel}
				<span class="meta-separator" aria-hidden="true">·</span>
				<span class="msg-model-badge" title={msg.metadata?.model ?? msg.metadata?.model_name}>
					{footerLabel}
				</span>
			{/if}
	</div>
</div>

<style>
	.sr-only {
		position: absolute; inline-size: 0; block-size: 0; margin: 0; padding: 0;
		overflow: hidden; clip-path: inset(50%); white-space: nowrap;
	}
	.tool-calls {
		display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px;
		padding-left: 8px; border-left: 2px solid color-mix(in srgb, var(--teal) 35%, transparent);
		border-radius: 1px;
	}

	.typewriter-text {
		margin: 0; padding: 0;
		font-family: inherit; font-size: inherit; line-height: inherit;
		color: inherit; background: none; border: none;
		white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word;
	}

	.reasoning-toggle {
		display: inline-flex; align-items: center; gap: 8px;
		background: var(--glass-thin);
		backdrop-filter: var(--glass-blur);
		-webkit-backdrop-filter: var(--glass-blur);
		border: 1px solid var(--border-subtle);
		box-shadow: var(--glass-rim);
		border-radius: var(--lg-card-radius); padding: 7px 12px;
		font-size: var(--text-footnote); color: var(--text-secondary);
		cursor: pointer; margin-bottom: 6px;
		transition: background var(--dur-tight);
	}
	.reasoning-toggle:hover { background: var(--bg-hover); }
	.reasoning-icon { opacity: 0.7; color: var(--text-tertiary); }
	.reasoning-chevron { transition: transform var(--dur-standard); }
	.reasoning-chevron.expanded { transform: rotate(180deg); }
	.reasoning-content {
		background: var(--bg-secondary); border-left: 2px solid color-mix(in srgb, var(--accent) 40%, transparent);
		padding: 8px 12px; margin-bottom: 8px; border-radius: var(--radius-2xs);
		max-height: 400px; overflow-y: auto;
	}

	.msg-footer { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
	.msg-time { font-size: var(--text-caption); color: var(--text-secondary); opacity: 0.7; }
	.msg-model-badge { font-size: var(--text-caption); color: var(--text-secondary); white-space: nowrap; }
	.meta-separator { color: var(--text-tertiary); }
	.typewriter-cursor {
		display: inline; color: var(--accent-text); font-weight: 300;
		animation: blink-cursor 0.8s step-end infinite;
	}
	@keyframes blink-cursor { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
	@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
</style>
