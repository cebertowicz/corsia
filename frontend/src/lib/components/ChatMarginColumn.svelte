<script lang="ts">
	import { messages, type ChatComment, type Message } from '$lib/stores/threads';
	import { sendCommentReply, type ChatComment as ReplyDraft } from '$lib/chatCommentAnchors';
	import type { MarginAnchorSource } from '$lib/docMarginSource';
	import DocMarginStack from './DocMarginStack.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';
	import { commentLanding } from './chatUnreadTargets';

	/**
	 * The chat comment threads as cards in the transcript's right margin, each beside the message its text
	 * was cut from: the document's margin column (`DocMarginStack`) with this transcript as its anchor source,
	 * so the close, shift and room rules are the document's. Which thread is picked belongs to the caller,
	 * because picking it on its highlight happens on the transcript.
	 */
	let { threadId, scroller, selectedId, onSelect, onSendReply }: {
		threadId: string;
		/** The transcript the cards ride. */
		scroller: HTMLElement | null;
		selectedId: string | null;
		onSelect: (id: string) => void;
		onSendReply: (reply: ReplyDraft) => void;
	} = $props();
	/**
	 * Every comment of this thread's loaded messages, in transcript order: every one is a card. A store change that leaves
	 * them and their message's place alone (a streaming reply's every tick) keeps the same list, so the column is not rebuilt for it.
	 */
	type Entry = { id: string; message: Message; comment: ChatComment };
	let shown: Entry[] = [];
	const entries = $derived.by(() => {
		const next: Entry[] = $messages
			.filter(message => message.thread_id === threadId)
			.flatMap(message => (message.chat_comments ?? []).map(comment => ({ id: comment.id, message, comment })));
		const same = next.length === shown.length && next.every((item, i) => item.comment === shown[i].comment && item.message.id === shown[i].message.id && item.message.seq === shown[i].message.seq);
		return same ? shown : (shown = next);
	});
	const anchors: MarginAnchorSource = {
		scroller: () => scroller,
		clip: (stack) => stack.closest<HTMLElement>('.chat-margin-clip'),
		// Where the comment is drawn: its highlight, or the message body its text left once nothing is painted any more.
		anchor: (id) => {
			const entry = entries.find(item => item.id === id);
			return entry && scroller ? commentLanding(scroller, entry.message.id, entry.comment) : null;
		},
		moved: () => (scroller ? [scroller] : []),
		// Its rows: one that grows with no change to the DOM (an image loading) moves every highlight below it.
		resized: () => (scroller ? [...scroller.children] : []),
		room: () => (scroller ? { el: scroller, property: '--margin-overhang' } : null),
	};
</script>

<div class="chat-margin">
	<div class="chat-margin-clip">
		<DocMarginStack source={anchors} pinned={entries} {selectedId} behind={false}>
			{#snippet card(entry)}
				<!-- Picking the card is the same pick as its highlight; the thread inside it owns the keyboard, and moving into it (its Reply field included) picks the card, as a document card does, so the stack keeps it in view. -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div class="chat-margin-card" role="presentation" class:selected={selectedId === entry.id} onclick={() => onSelect(entry.id)} onfocusin={() => onSelect(entry.id)}>
					<ChatCommentThreads messageId={entry.message.id} {threadId} comments={[entry.comment]} margin onReply={(text) => sendCommentReply(entry.message, entry.comment, text, onSendReply)} />
				</div>
			{/snippet}
		</DocMarginStack>
	</div>
</div>

<style>
	/* The transcript's own column, ending above the composer (its rule included, since the composer spans the whole chat) so no card hides
	   behind it, as the document's column ends above its footer. Its gutters are the document column's. */
	.chat-margin {
		flex: none; width: var(--margin-column); min-height: 0; display: flex; flex-direction: column;
		border-left: 1px solid var(--border-subtle); padding: 0 var(--space-2) 0 var(--space-3); margin-bottom: calc(var(--input-bar-height, 100px) + var(--space-4));
	}
	.chat-margin-clip { position: relative; flex: 1; min-height: 0; overflow: clip; }
	/* The document margin card's treatment; the thread itself stays flat inside it. */
	.chat-margin-card { padding: var(--space-2) var(--space-3); background: var(--glass-thin); border: 1px solid var(--border); border-radius: var(--radius-xl); box-shadow: var(--glass-rim); }
	/* Picked: the accent border and the app's own focus ring, the same signal a focused surface gives. */
	.chat-margin-card.selected { border-color: var(--accent); box-shadow: var(--focus-ring); }
</style>
