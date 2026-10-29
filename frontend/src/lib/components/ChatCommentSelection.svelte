<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { messages } from '$lib/stores/threads';
	import { clipChatSelection, type ChatComment, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
	import { keepingSelection, unpaint, wrapRange } from '$lib/cm/widget-paint';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import './commentAnchors.css';

	let { threadId, comments, onChange, surface }: { threadId: string | null; comments: ChatComment[]; onChange: (comments: ChatComment[]) => void; surface?: HTMLElement } = $props();
	function messageRoot(): HTMLElement | null {
		return surface?.closest('.subagent-conversation')?.querySelector<HTMLElement>('.messages') ?? document.querySelector<HTMLElement>('.messages');
	}
	type Pick = { messageId: string; seq: number; anchor: ChatCommentAnchor; rect: DOMRect; root_user_message_id?: string; root_cid?: string };
	let pick = $state<Pick | null>(null);
	let draft = $state<Pick | null>(null);
	let editingId = $state<string | null>(null);
	let buttonWidth = $state(0);
	let narrow = $state(false);
	const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
	const editing = $derived(comments.find(comment => comment.id === editingId));
	const open = $derived(!!draft || !!editing);
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
	const anchor = $derived.by(() => {
		if (draft) return draft.rect;
		if (!editing) return null;
		return () => [...document.querySelectorAll<HTMLElement>('[data-comment-ids]')].find(el => el.dataset.commentIds?.split(',').includes(editing.id))?.getBoundingClientRect()
			?? bodyOf(document.querySelector(`[data-message-id="${CSS.escape(editing.messageId)}"]`))?.getBoundingClientRect() ?? null;
	});

	function bodyOf(row: Element | null): HTMLElement | null {
		return row?.querySelector<HTMLElement>('.long-message-body, .bubble-content') ?? null;
	}
	function close() {
		pick = null; draft = null; editingId = null;
		window.getSelection()?.removeAllRanges();
		const focus = returnFocus; returnFocus = null;
		if (focus) void tick().then(() => focus.focus());
	}
	function requestClose() { if (!closing) { afterClose = close; closing = true; } }
	function closed() { const finish = afterClose; afterClose = null; finish?.(); closing = false; }
	function beginReply(message: (typeof $messages)[number], comment: NonNullable<(typeof $messages)[number]['chat_comments']>[number], target: HTMLElement) {
		if (typeof message.seq !== 'number') return;
		returnFocus = target;
		draft = { messageId: message.id, seq: message.seq, anchor: { from_off: comment.from_off, to_off: comment.to_off, quote: comment.quote, anchor_prefix: comment.anchor_prefix, anchor_suffix: comment.anchor_suffix }, rect: target.getBoundingClientRect(), root_user_message_id: comment.user_message_id, root_cid: comment.cid };
	}
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
	let observer: MutationObserver | undefined;
	let painting = false;
	function paint() {
		const root = messageRoot();
		if (!root) return;
		painting = true;
		const byId = new Map<string, (typeof $messages)[number]>();
		for (const message of $messages) {
			if (message.thread_id !== threadId) continue;
			if (!byId.has(message.id)) byId.set(message.id, message);
		}
		for (const row of root.querySelectorAll<HTMLElement>('[data-message-id]')) {
			const body = bodyOf(row);
			if (!body) continue;
			if (body.querySelector('.cm-comment-wrap')) keepingSelection(body, () => unpaint(body));
			const target = byId.get(row.dataset.messageId ?? '');
			const sent = target?.chat_comments ?? [];
			const pending = comments.filter(comment => comment.messageId === row.dataset.messageId);
			const marks = [...sent.map(comment => ({ ...comment, kind: 'placed' as const })), ...pending.map(comment => ({ ...comment, kind: editingId === comment.id ? 'focused' as const : 'pending' as const }))];
			if (draft && draft.messageId === row.dataset.messageId) marks.push({ ...draft.anchor, id: 'draft', kind: 'pending' } as typeof marks[number]);
			for (const comment of marks) {
				if ((body.textContent ?? '').slice(comment.from_off, comment.to_off) !== comment.quote) continue;
				const range = document.createRange();
				range.setStart(...point(body, comment.from_off)); range.setEnd(...point(body, comment.to_off));
				keepingSelection(body, () => wrapRange(range, { from: comment.from_off, to: comment.to_off, id: comment.id, kind: comment.kind }, comment.id !== 'draft'));
			}
		}
		observer?.takeRecords(); painting = false;
	}
	let queued = false;
	function schedule() {
		if (queued) return;
		queued = true;
		void tick().then(() => requestAnimationFrame(() => { queued = false; paint(); }));
	}
	$effect(() => { void comments; void editingId; void draft; void $messages; schedule(); });
	$effect(() => { void threadId; close(); });
	onMount(() => {
		const update = () => { narrow = window.innerWidth <= 768; };
		update(); window.addEventListener('resize', update);
		document.addEventListener('selectionchange', readSelection);
		const replyClick = (event: Event) => {
			const { threadId: sourceThread, messageId, commentId, target } = (event as CustomEvent<{ threadId: string; messageId: string; commentId: string; target: HTMLElement }>).detail;
			const message = $messages.find(item => item.id === messageId && item.thread_id === threadId);
			const comment = message?.chat_comments?.find(item => item.id === commentId);
			if (open || sourceThread !== threadId || !messageRoot()?.contains(target)) return;
			if (message && comment) beginReply(message, comment, target);
		};
		const click = (event: MouseEvent) => {
			const root = messageRoot();
			const target = event.target as Element | null;
			const hit = target?.closest<HTMLElement>('[data-comment-ids]');
			if (open || !hit || !(document.getSelection()?.isCollapsed ?? true) || !root?.contains(hit)) return;
			const source = hit;
			const ids = hit.dataset.commentIds?.split(',') ?? [];
			const pending = comments.find(comment => ids.includes(comment.id));
			if (pending) { returnFocus = source; editingId = pending.id; return; }
			for (const message of $messages) {
				if (message.thread_id !== threadId) continue;
				const comment = message.chat_comments?.find(item => ids.includes(item.id));
				if (comment) { beginReply(message, comment, source); return; }
			}
		};
		document.addEventListener('click', click);
		document.addEventListener('chat-comment-reply', replyClick);
		observer = new MutationObserver(() => { if (!painting) schedule(); });
		const root = messageRoot();
		if (root) observer.observe(root, { childList: true, subtree: true, characterData: true });
		return () => { observer?.disconnect(); window.removeEventListener('resize', update); document.removeEventListener('selectionchange', readSelection); document.removeEventListener('click', click); document.removeEventListener('chat-comment-reply', replyClick); };
	});
	export async function edit(id: string) {
		const comment = comments.find(item => item.id === id);
		if (!comment) return;
		returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		editingId = id; await tick(); schedule();
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		document.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(comment.messageId)}"] .cm-comment-focused`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
	async function submit(note: string) {
		const currentDraft = draft; const currentEditing = editing;
		if (currentDraft) onChange([...comments, { id: crypto.randomUUID(), messageId: currentDraft.messageId, seq: currentDraft.seq, ...currentDraft.anchor, note, root_user_message_id: currentDraft.root_user_message_id, root_cid: currentDraft.root_cid }]);
		else if (currentEditing) onChange(comments.map(item => item.id === currentEditing.id ? { ...item, note } : item));
		afterClose = () => {
			returnFocus = null; close();
			void tick().then(() => surface?.querySelector<HTMLElement>('[contenteditable="plaintext-only"]')?.focus());
		};
		closing = true;
	}
</script>

{#if pick && !open}
	<button bind:offsetWidth={buttonWidth} class="sel-comment" class:above class:below={!above} style="top:{above ? pick.rect.top - 6 : pick.rect.bottom + 6}px; left:{left}px" onmousedown={event => event.preventDefault()} onclick={() => { draft = pick; }}>
		<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>Comment
	</button>
{/if}
{#if open}
	{#key editing?.id ?? draft?.anchor.from_off}
		{#if narrow}
			<AdaptiveDrawer ariaLabel={heading} {heading} doneLabel="Close" followKeyboard open={!closing} onClose={requestClose} onClosed={closed}>
				<CommentComposer quote={editing?.quote ?? draft?.anchor.quote ?? ''} body={editing?.note ?? ''} fieldLabel={isReply ? 'Reply' : 'Comment'} placeholder={isReply ? 'Reply…' : 'Comment…'} submitLabel={editing ? 'Save' : isReply ? 'Reply' : 'Comment'} submittingLabel={editing ? 'Saving…' : isReply ? 'Replying…' : 'Posting…'} onSubmit={submit} onCancel={requestClose} />
			</AdaptiveDrawer>
		{:else}
			<Popover {anchor} bounds={messageRoot()} placement="bottom" open={!closing} initialFocus="textarea" surfaceRole={null} onClose={requestClose} onClosed={closed}>
				<div role="dialog" aria-label={heading}>
					<CommentComposer quote={editing?.quote ?? draft?.anchor.quote ?? ''} body={editing?.note ?? ''} fieldLabel={isReply ? 'Reply' : 'Comment'} placeholder={isReply ? 'Reply…' : 'Comment…'} submitLabel={editing ? 'Save' : isReply ? 'Reply' : 'Comment'} submittingLabel={editing ? 'Saving…' : isReply ? 'Replying…' : 'Posting…'} onSubmit={submit} onCancel={requestClose} />
				</div>
			</Popover>
		{/if}
	{/key}
{/if}
