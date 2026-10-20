<script lang="ts">
	import { token } from '$lib/stores/auth';
	import { reasoningEffort, saveReasoningEffort } from '$lib/stores/threads';
	import { composerSelectionFor } from '$lib/stores/composerSelection';
	import { patchSubagentThread } from '$lib/stores/subagents';
	import { emitProductEvent } from '$lib/product_events';
	import type { ReasoningConfig } from '$lib/stores/models';
	import { reasoningSegments, resolveReasoningEffort } from './inputBarReasoning';

	let {
		threadId = undefined,
		reasoningConfig,
	}: {
		threadId?: string | null;
		reasoningConfig: ReasoningConfig | null;
	} = $props();

	// The effort of the thread this composer is bound to, which is a worker's own in a worker's pane.
	const sel = $derived($composerSelectionFor(threadId));

	const segments = $derived(reasoningSegments(reasoningConfig));
	const resolvedEffort = $derived(resolveReasoningEffort(sel.effort, reasoningConfig));
	const activeSegIndex = $derived.by(() => {
		if (segments.length === 0) return 0;
		const index = segments.findIndex(segment => segment.value === resolvedEffort);
		return index >= 0 ? index : 0;
	});

	let effortSegEl = $state<HTMLDivElement | undefined>(undefined);
	let segDragActive = $state(false);
	let segDragIndex = $state(0);
	let segTrackRect: DOMRect | null = null;

	function setEffort(level: string | null) {
		const sid = sel.threadId;
		const t = $token;
		// Only the open thread's effort lives in the store; a worker's is on its row (the list holds the rest).
		if (sel.isOpen) reasoningEffort.set(level);
		else if (sid) patchSubagentThread(sid, { reasoning_effort: level });
		emitProductEvent('ui', 'thinking_toggled', {
			thread_id: sid,
			effort: level,
			reasoning_shape: reasoningConfig?.type ?? null,
		});
		if (sid && t) saveReasoningEffort(sid, level, t);
	}

	function isSegActive(value: string | null) {
		const index = segments.findIndex(segment => segment.value === value);
		return index === activeSegIndex;
	}

	function indexFromX(clientX: number) {
		if (!segTrackRect || segments.length === 0) return 0;
		const rel = clientX - segTrackRect.left;
		const segWidth = segTrackRect.width / segments.length;
		const index = Math.round((rel - segWidth / 2) / segWidth);
		return Math.max(0, Math.min(segments.length - 1, index));
	}

	function onSegTouchStart(event: TouchEvent) {
		if (!effortSegEl || segments.length === 0) return;
		segTrackRect = effortSegEl.getBoundingClientRect();
		segDragActive = true;
		segDragIndex = indexFromX(event.touches[0].clientX);
	}

	function onSegTouchMove(event: TouchEvent) {
		if (!segDragActive) return;
		segDragIndex = indexFromX(event.touches[0].clientX);
	}

	function onSegTouchEnd() {
		if (!segDragActive) return;
		const segment = segments[segDragIndex];
		segDragActive = false;
		segTrackRect = null;
		if (segment) setEffort(segment.value);
	}

	const thumbStyle = $derived.by(() => {
		const index = segDragActive ? segDragIndex : activeSegIndex;
		if (segments.length === 0) return 'opacity: 0';
		const width = 100 / segments.length;
		return `left: ${index * width}%; width: ${width}%;`;
	});
</script>

<div class="action-sheet thinking-sheet">
	<div class="sheet-section">
		<div class="sheet-section-label">Thinking</div>
		<div
			class="effort-seg drag-track"
			class:drag-active={segDragActive}
			role="group"
			aria-label="Thinking level"
			bind:this={effortSegEl}
			ontouchstart={onSegTouchStart}
			ontouchmove={onSegTouchMove}
			ontouchend={onSegTouchEnd}
			ontouchcancel={onSegTouchEnd}
		>
			<div class="effort-thumb" style={thumbStyle}></div>
			{#each segments as segment (segment.value ?? 'auto')}
				<button
					type="button"
					class="seg-btn"
					class:on={!segDragActive && isSegActive(segment.value)}
					onclick={() => setEffort(segment.value)}
				>
					{segment.label}
				</button>
			{/each}
		</div>
	</div>
</div>
