<script lang="ts">
	// The thread row's Done check (AS16): a check in slot 1 when the thread's
	// plan has settled and nothing runs for it, its own Turn or a descendant's.
	// It shares slot 1 with the pause mark. A pause or a higher-precedence
	// user-action mark wins. Agent identity rows never show Done, and Done
	// never moves the row.
	import { threadPlanStates } from '$lib/stores/threadPlanStates';
	import { descendantRunning, threadActivity } from '$lib/stores/activity';
	import { slotThreeWord } from './threadRowMark';

	let { threadId, size = 12 }: { threadId: string; size?: number } = $props();

	const entry = $derived($threadPlanStates[threadId]);
	const done = $derived(
		Boolean(entry?.settled)
		&& !entry?.paused
		&& slotThreeWord(entry) === ''
		&& $threadActivity[threadId] !== 'active'
		&& !$descendantRunning.has(threadId),
	);
</script>

{#if done}
	<svg class="done-mark" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" role="img" aria-label="Done">
		<title>Done</title>
		<!-- Stroke is the token itself, not currentColor: an ancestor that colors
		     child svgs (as the rail does) must not tint this quiet mark. -->
		<path d="M20 6 9 17l-5-5" stroke="var(--text-tertiary)" />
	</svg>
{/if}

<style>
	.done-mark { flex-shrink: 0; }
</style>