<script lang="ts">
	import { messages, type ChatComment, type Message } from '$lib/stores/threads';
	import { sendCommentReply, type ChatComment as ReplyDraft } from '$lib/chatCommentAnchors';
	import type { MarginAnchorSource, MarginComposer } from '$lib/docMarginSource';
	import { marginOnCanvas } from '$lib/docMarginCanvas';
	import DocMarginStack from './DocMarginStack.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';
	import { commentLanding } from './chatUnreadTargets';

	/**
	 * The chat comment threads as cards in the transcript's right margin, each level with the text its comment was cut
	 * from: the document's margin column (`DocMarginStack`) with this transcript as its anchor source, so the close, shift
	 * and room rules are the document's. Like the document's column it is drawn on the canvas, inside the transcript's own
	 * scroller in the free space right of the centred transcript, so the cards scroll with their messages. Which thread is
	 * picked belongs to the caller, because picking it on its highlight happens on the transcript.
	 */
	let { threadId, scroller, selectedId, onSelect, onSendReply, composer = null }: {
		threadId: string;
		/** The transcript the cards ride. */
		scroller: HTMLElement | null;
		selectedId: string | null;
		onSelect: (id: string) => void;
		onSendReply: (reply: ReplyDraft) => void;
		/** A comment being written or edited on the transcript, drawn as a card level with its text. */
		composer?: MarginComposer | null;
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
		clip: (stack) => stack.closest<HTMLElement>('.chat-margin'),
		// The composer floats over the transcript's lower edge, so a picked card is brought above it.
		view: () => {
			if (!scroller) return null;
			const box = scroller.getBoundingClientRect();
			return { top: box.top, bottom: box.bottom - (parseFloat(getComputedStyle(scroller).getPropertyValue('--input-bar-height')) || 100) };
		},
		// Where the comment is drawn: its highlight, or the message body its text left once nothing is painted any more.
		anchor: (id) => {
			const entry = entries.find(item => item.id === id);
			return entry && scroller ? commentLanding(scroller, entry.message.id, entry.comment) : null;
		},
		moved: () => (scroller ? [scroller] : []),
		// Its rows: one that grows with no change to the DOM (an image loading) moves every highlight below it.
		resized: () => (scroller ? [...scroller.children].filter(row => !row.classList.contains('chat-margin')) : []),
		room: () => (scroller ? { el: scroller, property: '--margin-overhang' } : null),
	};
</script>

<section class="chat-margin" aria-label="Chat comments" use:marginOnCanvas={{ root: scroller?.parentElement ?? null, scroller: '.messages' }}>
	<DocMarginStack source={anchors} pinned={entries} {selectedId} {composer} behind={false}>
		{#snippet card(entry)}
			<!-- Picking the card is the same pick as its highlight; the thread inside it owns the keyboard, and moving into it picks the card, as a document card does, so the stack keeps it in view. -->
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div class="chat-margin-card" role="presentation" class:selected={selectedId === entry.id} onclick={() => onSelect(entry.id)} onfocusin={() => onSelect(entry.id)}>
				<ChatCommentThreads messageId={entry.message.id} {threadId} comments={[entry.comment]} margin picked={selectedId === entry.id} onReply={(text) => sendCommentReply(entry.message, entry.comment, text, onSendReply)} />
			</div>
		{/snippet}
	</DocMarginStack>
</section>

<style>
	/* Drawn in the transcript's scroller (`marginOnCanvas`), in the free space right of the centred transcript, with the
	   document column's gutters and no box, rule or heading of its own; until it is there it is not drawn. The chat shows
	   the column only where that space holds it whole (`chatCommentMargin`), so the transcript never moves for it. */
	.chat-margin { display: none; }
	.chat-margin:global(.on-canvas) {
		display: block; position: absolute; z-index: 1; top: 0;
		left: calc(50% + var(--transcript-max-width) / 2 + var(--space-3)); width: calc(var(--margin-column) - var(--space-3) - var(--space-2));
	}
	:global(.messages:has(> .chat-margin.on-canvas)) { position: relative; }
	/* The composer floats over the transcript's end, which the transcript keeps clear with its own bottom padding: a card on
	   the last message reaches past that end, so it keeps the same room below itself and can always be scrolled into view whole. */
	.chat-margin :global(.margin-card)::after { content: ''; position: absolute; top: 100%; left: 0; width: 1px; height: calc(var(--input-bar-height, 100px) + var(--space-4)); visibility: hidden; pointer-events: none; }
</style>
