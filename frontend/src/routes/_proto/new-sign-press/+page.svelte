<script lang="ts">
	import { onMount } from 'svelte';
	import { messages, type Message } from '$lib/stores/threads';
	import ChatCommentSelection from '$lib/components/ChatCommentSelection.svelte';
	import '$lib/components/commentAnchors.css';

	// PROTOTYPE (uncommitted): the real chat comment surface over a transcript where two unread threads share text, to try a press on it.
	const params = new URLSearchParams(location.search);
	const theme = params.get('theme') ?? 'light';
	document.documentElement.setAttribute('data-theme', theme);
	const THREAD = 't-press';
	const sidebar = innerWidth > 1024 ? 280 : 0;

	type Mark = { quote: string; note: string; reply: string };
	const rows: { id: string; text: string; marks: Mark[] }[] = [
		{ id: 'm-nested', text: 'Legal still has to confirm the contract renewal date before Dana Okafor signs anything, and she will not move until it arrives on Friday afternoon.',
			marks: [{ quote: 'confirm the contract renewal date', note: 'Who is waiting on legal for this?', reply: 'Dana Okafor is, and the renewal is blocked on it.' }, { quote: 'renewal date', note: 'Is Friday still the date?', reply: 'Legal has only promised Friday afternoon.' }] },
		{ id: 'm-partial', text: 'Before Friday we need three things settled: the contract renewal date from legal, the final headcount for Lisbon, and a second pass on the migration checklist from Marcus Chen. Everything else in the plan can slip a day.',
			marks: [{ quote: 'a second pass on the migration', note: 'What does a second pass cover?', reply: 'Every step that touches the Lisbon office network.' }, { quote: 'migration checklist from Marcus Chen', note: 'Has Marcus seen this list?', reply: 'He has the draft and will confirm Thursday.' }] },
		{ id: 'm-single', text: 'Dana Okafor still needs the headcount for the Lisbon office, and Marcus Chen is waiting on the final budget line.',
			marks: [{ quote: 'final budget line', note: 'Which budget line is final?', reply: 'The vendor line, as of this morning.' }] },
	];
	const stamp = (n: number) => new Date(Date.UTC(2026, 10, 11, 18, n)).toISOString();
	const rowsToMessages = (): Message[] => rows.map((row, i) => ({
		id: row.id, thread_id: THREAD, role: 'assistant', content: row.text, created_at: stamp(i), seq: i + 2,
		chat_comments: row.marks.map((mark, j) => {
			const from = row.text.indexOf(mark.quote);
			return {
				id: `${row.id}-${j}`, cid: `c-${row.id}-${j}`, user_message_id: `u-${row.id}-${j}`, quote: mark.quote, note: mark.note,
				from_off: from, to_off: from + mark.quote.length, anchor_prefix: row.text.slice(Math.max(0, from - 8), from), anchor_suffix: row.text.slice(from + mark.quote.length, from + mark.quote.length + 8), created_at: stamp(30 + j),
				answers: [{ user_message_id: `u-${row.id}-${j}`, cid: `c-${row.id}-${j}`, author_kind: 'agent' as const, assistant_message_id: `a-${row.id}-${j}`, text: mark.reply, created_at: stamp(40 + j), unread: true }],
			};
		}),
	} as Message));
	messages.set(rowsToMessages());
	let scroller = $state<HTMLElement | undefined>();
	onMount(() => { document.documentElement.dataset.ready = '1'; });
</script>

<div class="app" style:--sidebar={`${sidebar}px`}>
	{#if sidebar}<aside class="sidebar" aria-hidden="true"></aside>{/if}
	<main class="messages" bind:this={scroller}>
		<div class="transcript">
			{#each rows as row (row.id)}
				<div class="message assistant" data-message-id={row.id}>
					<div class="comment-mark-layer"><div class="md-content long-message-body"><p>{row.text}</p></div></div>
					<div class="msg-footer"><span class="msg-time">4:49 PM</span></div>
				</div>
			{/each}
		</div>
	</main>
</div>
<ChatCommentSelection threadId={THREAD} comments={[]} onChange={() => {}} onSendReply={() => {}} surface={scroller} />

<style>
	.app { display: flex; height: 100vh; background: var(--bg-primary); color: var(--text-primary); }
	.sidebar { width: var(--sidebar); flex: none; }
	.messages { flex: 1; min-width: 0; overflow-y: auto; padding: 24px 16px 40px; }
	@media (max-width: 768px) { .messages { padding-left: 12px; padding-right: 12px; } }
	.transcript { max-width: var(--transcript-max-width); margin: 0 auto; }
	.message { margin: 0 0 var(--space-4); }
	.msg-footer { margin-top: 4px; }
	.msg-time { font-size: var(--text-caption); color: var(--text-secondary); opacity: 0.7; }
	.comment-mark-layer { position: relative; }
</style>
