<script lang="ts">
	import { onMount } from 'svelte';
	import { messages, type Message } from '$lib/stores/threads';
	import ChatCommentSelection from '$lib/components/ChatCommentSelection.svelte';
	import '$lib/components/commentAnchors.css';

	// PROTOTYPE (uncommitted): the real chat comment surface over stacks of 2 to 7 threads on one phrase, some read and some unread. Checks that a stack with any unread thread shows the unread underline, and that a press on the seven-thread stack keeps its text above the sheet.
	const params = new URLSearchParams(location.search);
	const theme = params.get('theme') ?? 'light';
	document.documentElement.setAttribute('data-theme', theme);
	const THREAD = 't-stack';
	const sidebar = innerWidth > 1024 ? 280 : 0;

	/** `unread` lists the 0-based numbers of the threads in the stack that have an unread reply. */
	type Stack = { id: string; text: string; quote: string; count: number; unread: number[] };
	const rows: Stack[] = [];
	for (let depth = 2; depth <= 7; depth++) {
		rows.push({ id: `s${depth}-first`, text: `Stack of ${depth}, only the first thread unread: please recheck the rollout window before Friday and tell the owner.`, quote: 'rollout window', count: depth, unread: [0] });
		rows.push({ id: `s${depth}-last`, text: `Stack of ${depth}, only the last thread unread: please recheck the rollout window before Friday and tell the owner.`, quote: 'rollout window', count: depth, unread: [depth - 1] });
	}
	rows.push({ id: 's7-mid', text: 'Stack of 7, threads 3 and 5 unread: please recheck the rollout window before Friday and tell the owner.', quote: 'rollout window', count: 7, unread: [2, 4] });
	rows.push({ id: 's7-allread', text: 'Stack of 7, all read: please recheck the rollout window before Friday and tell the owner.', quote: 'rollout window', count: 7, unread: [] });
	rows.push({ id: 'press7', text: 'Land the announcement with every channel on the same day, and tell each partner team before the public post goes out so nobody hears it second hand.', quote: 'every channel on the same day', count: 7, unread: [1, 4] });
	const stamp = (n: number) => new Date(Date.UTC(2026, 10, 11, 18, n)).toISOString();
	const toMessages = (): Message[] => rows.map((row, i) => ({
		id: row.id, thread_id: THREAD, role: 'assistant', content: row.text, created_at: stamp(i), seq: i + 2,
		chat_comments: Array.from({ length: row.count }, (_, j) => {
			const from = row.text.indexOf(row.quote);
			return {
				id: `${row.id}-${j}`, cid: `c-${row.id}-${j}`, user_message_id: `u-${row.id}-${j}`, quote: row.quote, note: `Question ${j + 1} about this wording?`,
				from_off: from, to_off: from + row.quote.length, anchor_prefix: row.text.slice(Math.max(0, from - 8), from), anchor_suffix: row.text.slice(from + row.quote.length, from + row.quote.length + 8), created_at: stamp(30 + j),
				answers: [{ user_message_id: `u-${row.id}-${j}`, cid: `c-${row.id}-${j}`, author_kind: 'agent' as const, assistant_message_id: `a-${row.id}-${j}`, text: `Answer ${j + 1}: yes, keep it.`, created_at: stamp(40 + j), unread: row.unread.includes(j) }],
			};
		}),
	} as Message));
	messages.set(toMessages());
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
