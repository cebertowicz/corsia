<script lang="ts">
	// The server's one highest-precedence mark for a thread (AS3): your move,
	// then Failed, then the update dot. Motion and pause remain in their own row
	// slots; the list overlay mounts at the sidebar root.
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { activeThreadId } from '$lib/stores/threads';
	import { chatStateTitle } from '$lib/components/plans/planDisplay';
	import { emitProductEvent } from '$lib/product_events';
	import { openAtDivider } from '$lib/stores/threadUpdateCards';
	import { isYourMove as owes, slotThreeWord } from './threadRowMark';

	const UPDATE_NAME = 'Update since your last visit';

	let {
		threadId,
		onOpenAttention,
		onOpenThread,
	}: {
		threadId: string;
		onOpenAttention?: (threadId: string, title: string) => void;
		/** Opens the thread, where its update is; the dot's action. */
		onOpenThread?: (threadId: string) => boolean | void | Promise<boolean | void>;
	} = $props();

	const entry = $derived($threadPlanStates[threadId]);
	// Retain the old generic word while a stale cached row entry is replaced.
	const count = $derived((entry?.attention_items ?? []).length);
	const title = $derived(entry && owes(entry) ? chatStateTitle(entry) : entry?.state === 'failed' ? 'Failed work' : '');
	const word = $derived(slotThreeWord(entry));
	// The thread in front of the user shows no dot while its visit commits.
	const updateDot = $derived(!word && Boolean(entry?.has_update) && $activeThreadId !== threadId);

	function show(event: Event): void {
		event.stopPropagation();
		emitProductEvent('sidebar', 'attention_opened', { thread_id: threadId, state: entry?.state, word, count });
		onOpenAttention?.(threadId, word);
	}

	function clearDividerOpen(): void {
		openAtDivider.update((open) => open === threadId ? null : open);
	}

	function reportOpen(opened: boolean | void): void {
		if (opened === false) {
			clearDividerOpen();
			emitProductEvent('sidebar', 'update_open', { outcome: 'refused', reason: 'not_opened' });
			return;
		}
		openAtDivider.set(threadId);
		emitProductEvent('sidebar', 'update_open', { outcome: 'succeeded' });
	}

	function reportOpenFailure(error: unknown): void {
		clearDividerOpen();
		emitProductEvent('sidebar', 'update_open', { outcome: 'failed', reason: 'open_failed' });
		// Keep a rejected navigation visible after observing it; analytics is never a recovery path.
		if (typeof reportError === 'function') reportError(error);
		else console.error('Thread open failed', error);
	}

	function openThread(event: Event): void {
		event.stopPropagation();
		if (!onOpenThread) {
			reportOpen(false);
			return;
		}
		try {
			const opened = onOpenThread(threadId);
			if (opened instanceof Promise) {
				void opened.then(reportOpen, reportOpenFailure);
				return;
			}
			reportOpen(opened);
		} catch (error) {
			reportOpenFailure(error);
			throw error;
		}
	}

	function activate(event: KeyboardEvent, action: (event: Event) => void): void {
		if (event.key !== 'Enter' && event.key !== ' ') return;
		event.preventDefault();
		action(event);
	}
</script>

{#if word && count === 0}
	<!-- Nothing listable behind it: a word, not a control that opens an empty list. -->
	<span class="plan-state static {entry?.state}" {title}>{word}</span>
{:else if word}
	<span
		class="plan-state {entry?.state}"
		role="button"
		tabindex="0"
		{title}
		aria-label={`${word}: ${count} item${count === 1 ? '' : 's'}`}
		onclick={show}
		onkeydown={(event) => activate(event, show)}
		onpointerdown={(event) => event.stopPropagation()}
	>{word}</span>
{:else if updateDot}
	<span
		class="plan-state update-dot"
		role="button"
		tabindex="0"
		title={UPDATE_NAME}
		aria-label={UPDATE_NAME}
		onclick={openThread}
		onkeydown={(event) => activate(event, openThread)}
		onpointerdown={(event) => event.stopPropagation()}
	></span>
{/if}

<style>
	.plan-state {
		flex-shrink: 0;
		font-size: var(--text-caption);
		line-height: 1;
		color: var(--text-tertiary);
		white-space: nowrap;
		cursor: pointer;
	}
	.plan-state.static { cursor: default; }
	.plan-state:focus-visible { outline: 2px solid var(--violet); outline-offset: 2px; border-radius: var(--radius-2xs); }
	.plan-state.needs_input, .plan-state.blocked_on_you { color: var(--amber); }
	.plan-state.blocked_on_you { font-weight: 600; }
	.plan-state.failed { color: var(--red); }
	.update-dot {
		width: 8px; height: 8px; border-radius: var(--radius-circle);
		background: var(--accent);
	}
	.update-dot:focus-visible { border-radius: var(--radius-circle); }
	@media (max-width: 768px) {
		.plan-state[role="button"] { position: relative; }
		/* Expand only the pointer target to at least 44px, whatever the mark's size;
		   the mark and its focus ring keep their visual size. */
		.plan-state[role="button"]::before {
			content: ""; position: absolute; top: 50%; left: 50%;
			width: max(calc(100% + 20px), 44px); height: max(100%, 44px);
			transform: translate(-50%, -50%);
		}
	}
</style>
