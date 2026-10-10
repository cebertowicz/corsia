<script lang="ts">
	import TranscriptContent from '$lib/components/TranscriptContent.svelte';
	import { buildTranscriptPresentation } from '$lib/components/transcriptFocusModel';
	import { mergeTranscriptUpdates } from '$lib/components/transcriptUpdates';
	import { planLimitPauseDescriptors } from '$lib/components/planLimitPause';
	import type { TranscriptMessage } from '$lib/components/messageListModel';
	import type { UpdateCard } from '$lib/api/threadUpdates';
	const thread = 'design-outcomes';
	const rows: TranscriptMessage[] = [1, 2, 3].map(seq => ({
		id: `design-work-${seq}`, thread_id: thread, seq, role: 'assistant', content: `Example work ${seq}`,
		created_at: '2026-09-29T10:00:00', activity_weight: 1, metadata: { turn_id: seq < 3 ? 'stopped' : 'next' },
	}));
	const summaries = {
		stopped: { status: 'cancelled', cause: 'plan_limit_overage_off', started_at: '2026-09-29T09:00:00', ended_at: '2026-09-29T10:00:00', activity_count: 2 },
		next: { status: 'completed', started_at: '2026-09-29T10:00:00', ended_at: '2026-09-29T10:05:00', activity_count: 1 },
	};
	const cards: UpdateCard[] = [
		{ id: 1, kind: 'done', title: 'Original task title with a longer explanation that wraps on a phone', after_seq: 1, created_at: '2026-09-29T09:00:01.123456', node_exists: false, asks_user: false, blocked_asks_user: false, node_id: null, thread_id: thread },
		{ id: 2, kind: 'done_follow_up', title: 'A follow-up was added', after_seq: 2, created_at: '2026-09-29T09:00:01.654321', node_exists: false, asks_user: false, blocked_asks_user: false, node_id: null, thread_id: thread },
		{ id: 3, kind: 'closed', title: 'Closed historical snapshot', after_seq: 3, created_at: '2026-09-29T10:01:00', node_exists: false, asks_user: false, blocked_asks_user: false, node_id: null, thread_id: thread },
		{ id: 4, kind: 'done', title: 'Timestamp unavailable example', after_seq: 3, created_at: 'invalid', node_exists: false, asks_user: false, blocked_asks_user: false, node_id: null, thread_id: thread },
	];
	const entries = mergeTranscriptUpdates(buildTranscriptPresentation(rows, true), cards, {
		focusMode: true, divider: null, pauses: planLimitPauseDescriptors(rows, summaries), turnSummaries: summaries,
	});
</script>

<main>
	<a href="/_design">Design system</a>
	<h1>Passive task outcomes</h1>
	<p>Work, original task snapshots and a canonical pause, using the composed transcript renderer.</p>
	<TranscriptContent {entries} expandedTools={new Set()} expandedReasoning={new Set()} onToggleToolBlock={() => {}} onToggleReasoning={() => {}} onOpenLightbox={() => {}} threadId={thread} renderedStreaming={false} turnSummaries={summaries} historyComplete />
</main>

<style>
	main { max-width: 900px; margin: 0 auto; padding: 24px 16px; }
	h1 { font-size: var(--text-title2); color: var(--text-primary); }
	p { color: var(--text-secondary); }
	a { color: var(--text-primary); }
</style>
