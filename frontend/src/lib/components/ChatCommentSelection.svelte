<script lang="ts">
	import { onMount, tick, untrack } from 'svelte';
	import { messages } from '$lib/stores/threads';
	import { chatReplyIndex } from '$lib/chatCommentDelivery';
	import { clipChatSelection, sendCommentReply, type ChatComment, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
	import { keepingSelection, unpaint, wrapRange } from '$lib/cm/widget-paint';
	import { chatCommentPlacement } from './chatCommentPlacement.svelte';
	import { commentLanding, quoteLabel, messageBody } from './chatUnreadTargets';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import ChatCommentThreads from './ChatCommentThreads.svelte';
	import { placeHighlightDot } from './comments/highlightDot';
	import { chatReplyDrafts } from './chatReplyDrafts.svelte';
	import type { MarginComposer } from '$lib/docMarginSource';
	import './commentAnchors.css';

	let { threadId, comments, onChange, onSendReply, surface, marginComments = false, selectedCommentId = $bindable(null), marginComposer = $bindable(null) }: { threadId: string | null; comments: ChatComment[]; onChange: (comments: ChatComment[]) => void; onSendReply: (reply: ChatComment) => void; surface?: HTMLElement; /** The threads show as cards in the transcript's margin column, so a highlight picks its card instead of opening a popover. */ marginComments?: boolean; /** The thread whose margin card is picked, shared with the column. */ selectedCommentId?: string | null; /** The comment being written, drawn by the column as a card level with its text while the column is up. */ marginComposer?: MarginComposer | null } = $props();
	function messageRoot(): HTMLElement | null {
		return surface?.closest('.subagent-conversation')?.querySelector<HTMLElement>('.messages') ?? document.querySelector<HTMLElement>('.messages');
	}
	type Pick = { messageId: string; seq: number; anchor: ChatCommentAnchor; rect: DOMRect; root_user_message_id?: string; root_cid?: string };
	let pick = $state<Pick | null>(null);
	let draft = $state<Pick | null>(null);
	let editingId = $state<string | null>(null);
	let viewing = $state<{ messageId: string; commentId: string } | null>(null);
	const viewedComment = $derived($messages.find(message => message.id === viewing?.messageId && message.thread_id === threadId)?.chat_comments?.find(comment => comment.id === viewing?.commentId));
	const threadAnchor = () => viewing ? commentRect(viewing.commentId, viewing.messageId) : null;
	// Text typed in the open popover, tied to its target; kept here because the narrow/wide branch remounts the composer.
	let typed = $state<{ target: string | number; text: string } | null>(null);
	let buttonWidth = $state(0);
	let narrow = $state(false);
	const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
	const editing = $derived(comments.find(comment => comment.id === editingId));
	const open = $derived(!!draft || !!editing);
	const target = $derived(editing?.id ?? draft?.anchor.from_off);
	const isReply = $derived(!!(editing?.root_cid ?? draft?.root_cid));
	const heading = $derived(editing ? isReply ? 'Edit reply' : 'Edit comment' : isReply ? 'Reply' : 'New comment');
	let returnFocus: HTMLElement | null = null;
	let closing = $state(false);
	let afterClose: (() => void) | null = null;
	const above = $derived(!!pick && !coarse && pick.rect.top > 96);
	const left = $derived.by(() => {
		if (!pick) return 0;
		const bounds = messageRoot()?.getBoundingClientRect();
		const wanted = pick.rect.right - buttonWidth;
		return bounds ? Math.max(bounds.left + 8, Math.min(wanted, bounds.right - 8 - buttonWidth)) : wanted;
	});
	// With the column up a comment is written in it, level with its text, so the text stays uncovered; there is no popover to close.
	const inMargin = $derived(open && marginComments && !narrow);
	$effect(() => { marginComposer = inMargin ? { id: 'composer', anchor: writingRect, body: marginBody } : null; });
	$effect(() => { if (closing && inMargin) closed(); });
	const anchor = $derived.by(() => {
		if (draft) return draft.rect;
		if (!editing) return null;
		return () => commentRect(editing.id, editing.messageId);
	});

	function bodyOf(row: Element | null): HTMLElement | null {
		return messageBody(row);
	}
	function commentRect(commentId: string, messageId: string): DOMRect | null {
		return commentLanding(document, messageId, { id: commentId })?.getBoundingClientRect() ?? null;
	}
	/** Where the comment being written is now: its highlight, which rides the transcript, not the place it was picked. */
	function writingRect(): DOMRect | null {
		if (editing) return commentRect(editing.id, editing.messageId);
		if (!draft) return null;
		const row = document.querySelector(`[data-message-id="${CSS.escape(draft.messageId)}"]`);
		return [...(row?.querySelectorAll<HTMLElement>('[data-comment-ids]') ?? [])].find(el => el.dataset.commentIds?.split(',').includes('draft'))?.getBoundingClientRect() ?? draft.rect;
	}
	function close() {
		pick = null; draft = null; editingId = null; if (viewing) chatReplyDrafts.set(viewing.commentId, ''); viewing = null; typed = null;
		window.getSelection()?.removeAllRanges();
		const focus = returnFocus; returnFocus = null;
		if (focus) void tick().then(() => focus.focus());
	}
	function requestClose() { if (!closing) { afterClose = close; closing = true; } }
	function closed() { const finish = afterClose; afterClose = null; finish?.(); closing = false; }
	function readSelection() {
		if (open) return;
		const selection = document.getSelection();
		if (!selection || selection.isCollapsed || !selection.rangeCount) { pick = null; return; }
		const start = selection.getRangeAt(0).startContainer;
		const element = start instanceof Element ? start : start.parentElement;
		const row = element?.closest<HTMLElement>('.messages .message.assistant[data-message-id]');
		const message = $messages.find(m => m.id === row?.dataset.messageId && m.thread_id === threadId);
		const body = bodyOf(row ?? null);
		if (!body || !message || typeof message.seq !== 'number') { pick = null; return; }
		const clipped = clipChatSelection(selection, body);
		if (!clipped) { pick = null; return; }
		if (!body.contains(selection.getRangeAt(0).endContainer)) {
			selection.removeAllRanges(); selection.addRange(clipped.range);
		}
		const rects = [...clipped.range.getClientRects()];
		if (!rects.length) { pick = null; return; }
		const { top, bottom, left, right } = rects.reduce((box, rect) => ({
			top: Math.min(box.top, rect.top), bottom: Math.max(box.bottom, rect.bottom),
			left: Math.min(box.left, rect.left), right: Math.max(box.right, rect.right),
		}), { top: Infinity, bottom: -Infinity, left: Infinity, right: -Infinity });
		pick = { messageId: message.id, seq: message.seq, anchor: clipped.anchor, rect: new DOMRect(left, top, right - left, bottom - top) };
	}
	function point(body: HTMLElement, offset: number): [Node, number] {
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		let remaining = offset;
		let last: Text | null = null;
		for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
			if (remaining <= node.length) return [node, remaining];
			remaining -= node.length; last = node;
		}
		return last ? [last, last.length] : [body, 0];
	}
	type PaintedMark = { from: number; to: number; quote: string; wraps: HTMLElement[]; labels: string[] };
	const painted = new WeakMap<HTMLElement, Map<string, PaintedMark>>();
	let observer: MutationObserver | undefined;
	let dotCleanups: (() => void)[] = [];
	let painting = false;
	/** The messages this surface recorded unplaced comments for, so closing it can clear them again. */
	let recorded: string[] = [];
	function recordUnplaced(messageId: string | undefined, sent: { id: string }[], placed: { id: string }[]): void {
		if (!messageId) return;
		// With the margin column up a comment whose text is gone keeps a card there, so nothing is unplaced: the row under the message is not its surface then.
		const left = marginComments ? [] : sent;
		const placedIds = new Set(placed.map(comment => comment.id));
		chatCommentPlacement.record(messageId, left.filter(comment => !placedIds.has(comment.id)).map(comment => comment.id));
		if (!recorded.includes(messageId)) recorded.push(messageId);
	}
	function paint() {
		const root = messageRoot();
		if (!root) return;
		// One pass over the loaded sends for the whole pass, read per comment below.
		const replies = threadId ? chatReplyIndex($messages, threadId) : null;
		const focusedMark = document.activeElement instanceof HTMLElement && root.contains(document.activeElement)
			? document.activeElement.closest<HTMLElement>('[data-comment-ids][role="button"]') : null;
		const focusedId = focusedMark?.dataset.commentIds?.split(',')[0];
		const focusedMessageId = focusedMark?.closest<HTMLElement>('[data-message-id]')?.dataset.messageId;
		painting = true;
		for (const cleanup of dotCleanups) cleanup();
		dotCleanups = [];
		const byId = new Map<string, (typeof $messages)[number]>();
		for (const message of $messages) {
			if (message.thread_id !== threadId) continue;
			if (!byId.has(message.id)) byId.set(message.id, message);
		}
		for (const row of root.querySelectorAll<HTMLElement>('[data-message-id]')) {
			const body = bodyOf(row);
			// With no body to measure there is no surface for a thread under this message either: show nothing rather than what an earlier pass found.
			if (!body) { recordUnplaced(row.dataset.messageId, [], []); continue; }
			let previous = painted.get(body);
			if (!previous) {
				if (body.querySelector('.cm-comment-wrap')) keepingSelection(body, () => unpaint(body));
				previous = new Map();
				painted.set(body, previous);
			}
			const target = byId.get(row.dataset.messageId ?? '');
			const sent = target?.chat_comments ?? [];
			const pending = comments.filter(comment => comment.messageId === row.dataset.messageId);
			const marks = [...sent.map(comment => ({ ...comment, kind: replies?.pending(comment).length ? 'pending' as const : 'placed' as const })), ...pending.map(comment => ({ ...comment, kind: editingId === comment.id ? 'focused' as const : 'pending' as const }))];
			if (draft && draft.messageId === row.dataset.messageId) marks.push({ ...draft.anchor, id: 'draft', kind: 'pending' } as typeof marks[number]);
			const valid = marks.filter(comment => (body.textContent ?? '').slice(comment.from_off, comment.to_off) === comment.quote);
			recordUnplaced(row.dataset.messageId, sent, valid);
			for (const [id, old] of previous) {
				const next = valid.find(comment => comment.id === id);
				const first = old.wraps[0];
				const before = document.createRange();
				if (first && body.contains(first)) { before.selectNodeContents(body); before.setEndBefore(first); }
				const unchanged = next && old.from === next.from_off && old.to === next.to_off && old.quote === next.quote
					&& old.wraps.length > 0 && old.wraps.every(wrap => body.contains(wrap))
					&& before.toString().length === next.from_off && old.wraps.map(wrap => wrap.textContent ?? '').join('') === next.quote;
				if (unchanged) continue;
				keepingSelection(body, () => { for (const wrap of old.wraps) if (body.contains(wrap)) wrap.replaceWith(...wrap.childNodes); });
				previous.delete(id);
			}
			for (const comment of valid) {
				let current = previous.get(comment.id);
				if (!current) {
					const priorWraps = new Set(body.querySelectorAll<HTMLElement>('.cm-comment-wrap'));
					const range = document.createRange();
					range.setStart(...point(body, comment.from_off)); range.setEnd(...point(body, comment.to_off));
					keepingSelection(body, () => wrapRange(range, { from: comment.from_off, to: comment.to_off, id: comment.id, kind: comment.kind }, comment.id !== 'draft'));
					const wraps = [...body.querySelectorAll<HTMLElement>('.cm-comment-wrap')].filter(wrap => !priorWraps.has(wrap));
					current = { from: comment.from_off, to: comment.to_off, quote: comment.quote, wraps, labels: wraps.map(wrap => wrap.getAttribute('aria-label') ?? '') };
					previous.set(comment.id, current);
				}
				for (const [index, wrap] of current.wraps.entries()) {
					wrap.classList.toggle('cm-comment-anchor', comment.kind !== 'pending');
					wrap.classList.toggle('cm-comment-focused', comment.kind === 'focused');
					wrap.classList.toggle('cm-comment-pending', comment.kind === 'pending');
					wrap.setAttribute('aria-label', current.labels[index]);
				}
				if (comment.id !== 'draft' && current.wraps[0]) {
					current.wraps[0].tabIndex = 0; current.wraps[0].setAttribute('role', 'button'); current.wraps[0].setAttribute('aria-haspopup', 'dialog');
				}
			}
			for (const comment of sent) {
				const failed = !!replies?.failed(comment).length;
				const sending = !!replies?.pending(comment).length;
				const unread = comment.answers.some(answer => answer.author_kind === 'agent' && answer.unread);
				if (!failed && !sending && !unread) continue;
				const wraps = [...body.querySelectorAll<HTMLElement>('[data-comment-ids]')].filter(el => el.dataset.commentIds?.split(',').includes(comment.id));
				const anchor = wraps.at(-1);
				if (anchor) {
					const quote = quoteLabel(comment.quote);
					wraps[0].setAttribute('aria-label', failed ? `Your reply on ${quote} wasn’t delivered.` : sending ? `Your reply on ${quote} is sending.` : `The comment on ${quote} has a new reply.`);
					if (failed || !sending) dotCleanups.push(placeHighlightDot(body, anchor, failed ? quote : null));
				}
			}
		}
		if (focusedId && focusedMessageId) {
			const row = root.querySelector<HTMLElement>(`.message[data-message-id="${CSS.escape(focusedMessageId)}"]`);
			const replacement = [...(row?.querySelectorAll<HTMLElement>('[data-comment-ids][role="button"]') ?? [])]
				.find(mark => mark.dataset.commentIds?.split(',').includes(focusedId));
			replacement?.focus({ preventScroll: true });
		}
		observer?.takeRecords(); painting = false;
	}
	let queued = false;
	function schedule() {
		if (queued) return;
		queued = true;
		void tick().then(() => requestAnimationFrame(() => { queued = false; paint(); }));
	}
	$effect(() => { void comments; void editingId; void draft; void $messages; void marginComments; schedule(); });
	// A picked card belongs to the transcript it was picked in, so changing thread drops the pick. Untracked: close() reads the open thread, and opening one must not close it.
	$effect(() => { void threadId; untrack(() => { close(); selectedCommentId = null; }); chatCommentPlacement.forget(recorded); recorded = []; });
	// The column coming up while a thread is open hands that thread to its card: picked there, with its unsent reply, so it is never open twice.
	$effect(() => { if (marginComments) untrack(() => { if (viewing) { selectedCommentId = viewing.commentId; viewing = null; returnFocus = null; } }); });
	onMount(() => {
		const update = () => { narrow = window.innerWidth <= 768; };
		update(); window.addEventListener('resize', update);
		document.addEventListener('selectionchange', readSelection);
		const replyClick = (event: Event) => {
			const { threadId: sourceThread, messageId, commentId, target } = (event as CustomEvent<{ threadId: string; messageId: string; commentId: string; target: HTMLElement }>).detail;
			const message = $messages.find(item => item.id === messageId && item.thread_id === threadId);
			const comment = message?.chat_comments?.find(item => item.id === commentId);
			if (open || sourceThread !== threadId || !messageRoot()?.contains(target)) return;
			// One reply behavior: a thread's Reply opens that comment's thread, whose send goes straight through onSendReply, the same as the popover and the drawer. It never routes through the composer.
			if (message && comment) { returnFocus = target; viewing = { messageId: message.id, commentId: comment.id }; }
		};
		const openHighlight = (ids: string[], source: HTMLElement) => {
			const pending = comments.find(comment => ids.includes(comment.id));
			if (pending) { returnFocus = source; editingId = pending.id; return; }
			for (const message of $messages) {
				if (message.thread_id !== threadId) continue;
				const comment = message.chat_comments?.find(item => ids.includes(item.id));
				if (!comment) continue;
				// With the column up the thread is already on screen beside its text: the highlight picks it, as a highlight picks a card in a document.
				if (marginComments) { selectedCommentId = comment.id; return; }
				returnFocus = source; viewing = { messageId: message.id, commentId: comment.id }; return;
			}
		};
		const click = (event: MouseEvent) => {
			const root = messageRoot();
			const target = event.target as Element | null;
			const hit = target?.closest<HTMLElement>('[data-comment-ids]');
			if (!hit || !(document.getSelection()?.isCollapsed ?? true) || !root?.contains(hit)) return;
			const ids = hit.dataset.commentIds?.split(',') ?? [];
			if (closing) {
				const finish = afterClose;
				afterClose = () => { finish?.(); openHighlight(ids, hit); };
				return;
			}
			if (!open) openHighlight(ids, hit);
		};
		const keydown = (event: KeyboardEvent) => {
			// In the column the thread is already on screen, so the first Escape lets the picked card go, as it does in a document's margin.
			if (event.key === 'Escape' && marginComments && !open && selectedCommentId) { selectedCommentId = null; returnFocus = null; return; }
			if ((event.key === 'Enter' || event.key === ' ') && (event.target as HTMLElement | null)?.matches('[data-comment-ids][role="button"]')) {
				event.preventDefault(); (event.target as HTMLElement).click();
			}
		};
		document.addEventListener('click', click);
		document.addEventListener('keydown', keydown);
		document.addEventListener('chat-comment-reply', replyClick);
		observer = new MutationObserver(records => {
			const changedContent = records.some(record => {
				const target = record.target instanceof Element ? record.target : record.target.parentElement;
				if (target?.closest('[data-highlight-dot]')) return false;
				const nodes = [...record.addedNodes, ...record.removedNodes];
				const row = target?.closest('.message[data-message-id]') ?? null;
				if (target && bodyOf(row)?.contains(target)) {
					return record.type === 'characterData' || nodes.some(node => !(node instanceof Element && node.matches('[data-highlight-dot]')));
				}
				return nodes.some(node => node instanceof Element && (
					node.matches('.message[data-message-id]') || !!node.querySelector('.message[data-message-id]') ||
					(!!row && (node.matches('.long-message-body, .bubble-content') || !!node.querySelector('.long-message-body, .bubble-content')))
				));
			});
			if (!painting && changedContent) schedule();
		});
		const root = messageRoot();
		if (root) observer.observe(root, { childList: true, subtree: true, characterData: true });
		return () => { for (const cleanup of dotCleanups) cleanup(); chatCommentPlacement.forget(recorded); recorded = []; observer?.disconnect(); window.removeEventListener('resize', update); document.removeEventListener('selectionchange', readSelection); document.removeEventListener('click', click); document.removeEventListener('keydown', keydown); document.removeEventListener('chat-comment-reply', replyClick); };
	});
	export async function edit(id: string) {
		const comment = comments.find(item => item.id === id);
		if (!comment) return;
		returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		editingId = id; await tick(); schedule();
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		document.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(comment.messageId)}"] .cm-comment-focused`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
	function replyInThread(note: string): void {
		// The row this thread opened on may have been replaced or removed while the reply was being typed.
		sendCommentReply($messages.find(item => item.id === viewing?.messageId && item.thread_id === threadId), viewedComment, note, onSendReply);
	}
	async function submit(note: string) {
		const currentDraft = draft; const currentEditing = editing;
		if (currentDraft) onChange([...comments, { id: crypto.randomUUID(), messageId: currentDraft.messageId, seq: currentDraft.seq, ...currentDraft.anchor, note, root_user_message_id: currentDraft.root_user_message_id, root_cid: currentDraft.root_cid }]);
		else if (currentEditing) onChange(comments.map(item => item.id === currentEditing.id ? { ...item, note } : item));
		afterClose = () => {
			returnFocus = null; close();
			void tick().then(() => {
				const active = document.activeElement;
				if (active === document.body || !active?.isConnected || active?.closest('[data-adaptive-drawer]')) {
					surface?.querySelector<HTMLElement>('[contenteditable="plaintext-only"]')?.focus();
				}
			});
		};
		closing = true;
	}
</script>

{#if pick && !open}
	<button bind:offsetWidth={buttonWidth} class="sel-comment" class:above class:below={!above} style="top:{above ? pick.rect.top - 6 : pick.rect.bottom + 6}px; left:{left}px" onmousedown={event => event.preventDefault()} onclick={() => { draft = pick; }}>
		<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>Comment
	</button>
{/if}
{#if viewing && viewedComment && threadId && !open}
	{#if narrow}
		<AdaptiveDrawer ariaLabel="Comment thread" heading="Comments" doneLabel="Close" followKeyboard open={!closing} onClose={requestClose} onClosed={closed}>
			<ChatCommentThreads messageId={viewing.messageId} {threadId} comments={[viewedComment]} onReply={replyInThread} keepReplyVisible />
		</AdaptiveDrawer>
	{:else}
		<Popover anchor={threadAnchor} bounds={messageRoot()} placement="bottom" open={!closing} surfaceRole={null} onClose={requestClose} onClosed={closed}>
			<div role="dialog" aria-label="Comment thread"><ChatCommentThreads messageId={viewing.messageId} {threadId} comments={[viewedComment]} onReply={replyInThread} /></div>
		</Popover>
	{/if}
{/if}
{#snippet composerBody()}
	<CommentComposer mentions={false} quote={editing?.quote ?? draft?.anchor.quote ?? ''} body={typed && typed.target === target ? typed.text : editing?.note ?? ''} onBody={text => { if (target !== undefined) typed = { target, text }; }} fieldLabel={isReply ? 'Reply' : 'Comment'} placeholder={isReply ? 'Reply…' : 'Comment…'} submitLabel={editing ? 'Save' : isReply ? 'Reply' : 'Comment'} submittingLabel={editing ? 'Saving…' : isReply ? 'Replying…' : 'Posting…'} onSubmit={submit} onCancel={requestClose} />
{/snippet}
{#snippet marginBody()}
	{#key target}<div role="dialog" aria-label={heading}>{@render composerBody()}</div>{/key}
{/snippet}
{#if open && !inMargin}
	{#key target}
		{#if narrow}
			<AdaptiveDrawer ariaLabel={heading} {heading} doneLabel="Close" followKeyboard open={!closing} onClose={requestClose} onClosed={closed}>
				{@render composerBody()}
			</AdaptiveDrawer>
		{:else}
			<Popover {anchor} bounds={messageRoot()} placement="bottom" open={!closing} initialFocus="textarea" surfaceRole={null} onClose={requestClose} onClosed={closed}>
				<div role="dialog" aria-label={heading}>
					{@render composerBody()}
				</div>
			</Popover>
		{/if}
	{/key}
{/if}
