<script lang="ts">
	import type { Thread } from '$lib/stores/threads';
	import {
		activeCheckpoint,
		checkpointsByThread,
	} from '$lib/stores/checkpoints';
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { descendantRunning } from '$lib/stores/activity';
	import ThreadPlanStateMark from './ThreadPlanStateMark.svelte';
	import { isYourMove } from './threadRowMark';
	import ThreadPausedMark from './ThreadPausedMark.svelte';
	import ThreadDoneMark from './ThreadDoneMark.svelte';

	interface Props {
		thread: Thread;
		isPinned: boolean;
		isActive: boolean;
		activity?: string;
		renaming: boolean;
		renameValue: string;
		swiping: boolean;
		isSwipeGesture: boolean;
		transformX: number;
		leftZoneX: number;
		rightZoneX: number;
		pinArmed: boolean;
		deleteArmed: boolean;
		onTouchStart: (e: TouchEvent, id: string) => void;
		onTouchEnd: () => void;
		onSelect: (id: string) => boolean | void | Promise<boolean | void>;
		onOpenAttention?: (threadId: string, title: string) => void;
		onOpenContextMenu: (e: MouseEvent, id: string) => void;
		onPin: (id: string, e: MouseEvent | TouchEvent) => void;
		onArchive: (id: string, e: MouseEvent | TouchEvent) => void;
		onDelete: (id: string, e: MouseEvent | TouchEvent) => void;
		onStartRename: (id: string) => void;
		onFinishRename: () => void;
		onCancelRename: () => void;
	}

	let {
		thread,
		isPinned,
		isActive,
		activity,
		renaming,
		renameValue = $bindable(''),
		swiping,
		isSwipeGesture,
		transformX,
		leftZoneX,
		rightZoneX,
		pinArmed,
		deleteArmed,
		onTouchStart,
		onTouchEnd,
		onSelect,
		onOpenAttention,
		onOpenContextMenu,
		onPin,
		onArchive,
		onDelete,
		onStartRename,
		onFinishRename,
		onCancelRename,
	}: Props = $props();

	const checkpoint = $derived(
		activeCheckpoint($checkpointsByThread.get(thread.id)),
	);
	const checkpointLabel = $derived(
		checkpoint?.trigger === 'model_switch' ? 'switching' : checkpoint ? 'checkpointing' : null,
	);
	const planState = $derived($threadPlanStates[thread.id]);
	const updateSummary = $derived(planState?.latest_update?.summary);
	// What the user owes takes the second line, in place of the update summary; the mark owns it either way.
	const needsYou = $derived(isYourMove(planState));
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="thread-item-wrapper"
	ontouchstart={(e: TouchEvent) => onTouchStart(e, thread.id)}
	ontouchend={onTouchEnd}
>
	<button
		class="action-zone left-zone"
		class:armed={pinArmed}
		class:moving={swiping}
		tabindex="-1"
		style={`transform: translate3d(${leftZoneX}px, 0, 0)`}
		ontouchstart={(e: TouchEvent) => e.stopPropagation()}
		ontouchend={(e: TouchEvent) => onPin(thread.id, e)}
		onclick={(e: MouseEvent) => onPin(thread.id, e)}
	>
		<svg width="16" height="16" viewBox="0 0 24 24" fill={isPinned ? 'currentColor' : 'none'} stroke="currentColor" stroke-width="2"><path d="M16 3a1 1 0 00-1 1v1.586l-6.293 6.293A1 1 0 008 12.586V15H5a1 1 0 000 2h2v2a1 1 0 002 0v-2h2.414a1 1 0 00.707-.293L18.414 11H20a1 1 0 001-1V4a1 1 0 00-1-1h-4z"/></svg>
		<span class="zone-label">{isPinned ? 'Unpin' : 'Pin'}</span>
	</button>
	<div
		class="right-zone-group"
		class:moving={swiping}
		style={`transform: translate3d(${rightZoneX}px, 0, 0)`}
	>
		<button
			class="action-zone archive-zone"
			tabindex="-1"
			ontouchstart={(e: TouchEvent) => e.stopPropagation()}
			ontouchend={(e: TouchEvent) => onArchive(thread.id, e)}
			onclick={(e: MouseEvent) => onArchive(thread.id, e)}
		>
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>
			<span class="zone-label">Archive</span>
		</button>
		<button
			class="action-zone delete-zone"
			class:armed={deleteArmed}
			tabindex="-1"
			ontouchstart={(e: TouchEvent) => e.stopPropagation()}
			ontouchend={(e: TouchEvent) => onDelete(thread.id, e)}
			onclick={(e: MouseEvent) => onDelete(thread.id, e)}
		>
			<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
			<span class="zone-label">Delete</span>
		</button>
	</div>
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="thread-item press-tint"
		class:active={isActive}
		class:swiping={swiping && isSwipeGesture}
		style={`transform: translate3d(${transformX}px, 0, 0)`}
		onclick={() => onSelect(thread.id)}
		oncontextmenu={(e: MouseEvent) => onOpenContextMenu(e, thread.id)}
	>
		<div class="thread-content">
			{#if renaming}
				<!-- svelte-ignore a11y_autofocus -->
				<input
					class="thread-rename-input"
					bind:value={renameValue}
					onblur={onFinishRename}
					onkeydown={(e: KeyboardEvent) => { if (e.key === 'Enter') onFinishRename(); if (e.key === 'Escape') onCancelRename(); }}
					onclick={(e: MouseEvent) => e.stopPropagation()}
					autofocus
				/>
			{:else}
				<div class="thread-title-row">
					{#if $threadPlanStates[thread.id]?.paused}
						<ThreadPausedMark size={12} />
					{:else}
						<ThreadDoneMark threadId={thread.id} />
					{/if}
					<div class="thread-title">{thread.title}</div>
					{#if checkpointLabel}
						<span
							class="thread-cutover"
						title={checkpoint?.trigger === 'model_switch'
							? 'Switching model'
							: 'Checkpointing context'}
						>{checkpointLabel}</span>
					{/if}
					{#if !needsYou}
						<ThreadPlanStateMark threadId={thread.id} {onOpenAttention} onOpenThread={onSelect} />
					{/if}
				</div>
				{#if needsYou}
					<ThreadPlanStateMark threadId={thread.id} line {onOpenAttention} onOpenThread={onSelect} />
				{:else if updateSummary}
					<span class="thread-update-summary" title={updateSummary}>{updateSummary}</span>
				{/if}
			{/if}
		</div>
		<!-- The trailing column: centred on the whole row, so one line or two, the spinner sits in the middle. -->
		{#if !renaming && activity === 'active'}
			<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--status-in-progress)" stroke-width="3" role="img" aria-label="Working"><title>Working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		{:else if !renaming && $descendantRunning.has(thread.id)}
			<svg class="spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-tertiary)" stroke-width="3" role="img" aria-label="Subagent working"><title>Subagent working</title><path d="M21 12a9 9 0 11-6.219-8.56"/></svg>
		{/if}
		<div class="thread-hover-actions">
			<button
				class="thread-action-btn"
				onclick={(e: MouseEvent) => { e.stopPropagation(); onStartRename(thread.id); }}
				title="Rename"
				aria-label="Rename conversation"
			>
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z"/></svg>
			</button>
		</div>
	</div>
</div>

<style>
	.thread-item-wrapper {
		position: relative; overflow: hidden; border-radius: var(--radius-sm); margin-bottom: 2px;
	}
	.action-zone {
		position: absolute; top: 0; bottom: 0;
		display: flex; flex-direction: column;
		align-items: center; justify-content: center; gap: 3px;
		color: white; border: none; cursor: pointer; font-family: inherit;
		padding: 0; z-index: 0;
		transition: transform var(--dur-standard) var(--ease-ios-sheet),
		            filter var(--dur-tight) ease-out;
		backface-visibility: hidden;
	}
	.action-zone.moving { transition: filter var(--dur-tight) ease-out; }
	.action-zone .zone-label { font-size: var(--text-footnote); font-weight: 600; letter-spacing: 0.01em; }
	.left-zone {
		left: 0; width: 80px;
		background: var(--accent-fill); color: var(--accent-on-fill);
		border-top-left-radius: var(--radius-sm);
		border-bottom-left-radius: var(--radius-sm);
	}
	.right-zone-group {
		position: absolute; top: 0; bottom: 0; right: 0;
		display: flex; z-index: 0;
		width: 144px;
		transition: transform var(--dur-standard) var(--ease-ios-sheet);
		backface-visibility: hidden;
	}
	.right-zone-group.moving { transition: none; }
	.archive-zone {
		position: relative; width: 72px;
		background: var(--amber);
	}
	.delete-zone {
		position: relative; width: 72px;
		background: var(--red);
		border-top-right-radius: var(--radius-sm);
		border-bottom-right-radius: var(--radius-sm);
	}
	.action-zone.armed { filter: brightness(1.12); }
	.thread-item {
		/* The row's height no longer comes from the out-of-flow action, so it is held at the list's 38px pitch. */
		width: 100%; min-height: 38px; text-align: left; padding: 7px 10px;
		border-radius: var(--radius-sm); cursor: pointer;
		background: transparent;
		border: none; color: var(--text-primary); font-family: inherit;
		display: flex; align-items: center; gap: 8px;
		position: relative; z-index: 1;
		transition: transform var(--dur-standard) var(--ease-ios-sheet),
		            background var(--dur-standard) ease-out;
		transform: translate3d(0, 0, 0);
		backface-visibility: hidden;
		-webkit-user-select: none;
		user-select: none;
		-webkit-touch-callout: none;
	}
	.thread-item.swiping {
		transition: none;
		will-change: transform;
	}
	.thread-content { flex: 1; min-width: 0; }
	/* Out of flow, so the hidden action reserves no width: the name and its second line run to the trailing edge. */
	.thread-hover-actions {
		position: absolute; top: 50%; right: 10px; transform: translateY(-50%);
		display: flex; align-items: center; gap: 2px;
		opacity: 0; pointer-events: none; transition: opacity var(--dur-tight);
	}
	.thread-hover-actions:focus-within { opacity: 1; pointer-events: auto; }
	@media (hover: hover) {
		.thread-item:hover .thread-hover-actions { opacity: 1; pointer-events: auto; }
		/* On hover the action takes the trailing slot: the text stops short of it and the spinner steps aside. */
		.thread-item:hover .thread-content { padding-right: 28px; }
		.thread-item:hover > .spinner { display: none; }
	}
	.thread-action-btn {
		width: 24px; height: 24px; border-radius: var(--radius-2xs);
		background: none; border: none; color: var(--text-tertiary);
		cursor: pointer; display: flex; align-items: center; justify-content: center;
		flex-shrink: 0; transition: background var(--dur-tight), color var(--dur-tight);
	}
	.thread-action-btn:hover { color: var(--text-primary); background: var(--bg-tertiary); }
	.thread-item:hover { background: var(--bg-hover); }
	.thread-item.active {
		background: var(--accent-wash);
	}
	.thread-title-row {
		display: flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%;
	}
	.thread-title {
		font-size: var(--text-subhead); font-weight: 500;
		white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
		flex: 1; min-width: 0;
	}
	.thread-update-summary {
		display: block; min-width: 0; margin-top: 1px;
		font-size: var(--text-caption); line-height: 1.2; color: var(--text-tertiary);
		white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
	}
	.thread-cutover {
		flex-shrink: 0;
		font-size: var(--text-caption);
		font-weight: 500;
		line-height: 1;
		padding: 2px 6px;
		border-radius: var(--radius-2xs);
		background: var(--bg-tertiary);
		color: var(--text-secondary);
		white-space: nowrap;
	}
	.thread-rename-input {
		width: 100%; padding: 2px 6px; font-size: var(--text-input); font-weight: 500;
		background: var(--bg-tertiary); border: 1px solid var(--accent);
		border-radius: var(--radius-2xs); color: var(--text-primary); font-family: inherit; outline: none;
	}

	.spinner {
		animation: spin 1s linear infinite;
		flex-shrink: 0;
	}
	@media (prefers-reduced-motion: reduce) {
		/* The arc stays, still titled, so reduced motion reads the same truth. */
		.spinner { animation: none; }
	}
	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
	@media (max-width: 768px) {
		.thread-hover-actions { display: none; }
		.thread-item {
			padding: 12px 12px;
			min-height: 44px;
		}
		.thread-item-wrapper {
			margin-bottom: 4px;
		}
	}
</style>
