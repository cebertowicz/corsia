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
	type Pick = { messageId: string; seq: number; anchor: ChatCommentAnchor; rect: DOMRect };
	let pick = $state<Pick | null>(null);
	let draft = $state<Pick | null>(null);
	let editingId = $state<string | null>(null);
	let buttonWidth = $state(0);
	let narrow = $state(false);
	const coarse = typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches;
	const editing = $derived(comments.find(comment => comment.id === editingId));
	const open = $derived(!!draft || !!editing);
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
		const sentBySeq = new Map<number | null | undefined, any[]>();
		for (const message of $messages) {
			if (message.thread_id !== threadId) continue;
			if (!byId.has(message.id)) byId.set(message.id, message);
			for (const comment of message.metadata?.reply_context?.comments ?? []) {
				const sent = sentBySeq.get(comment.seq) ?? [];
				sent.push(comment);
				sentBySeq.set(comment.seq, sent);
			}
		}
		for (const row of root.querySelectorAll<HTMLElement>('[data-message-id]')) {
			const body = bodyOf(row);
			if (!body) continue;
			if (body.querySelector('.cm-comment-wrap')) keepingSelection(body, () => unpaint(body));
			const target = byId.get(row.dataset.messageId ?? '');
			const indexed = target?.chat_comments ?? [];
			const sent = [...new Map([...indexed, ...(sentBySeq.get(target?.seq) ?? [])].map(comment => [comment.id, comment])).values()];
			const pending = comments.filter(comment => comment.messageId === row.dataset.messageId);
			const marks = [...sent.map(comment => ({ ...comment, kind: 'placed' as const })), ...pending.map(comment => ({ ...comment, kind: editingId === comment.id ? 'focused' as const : 'pending' as const }))];
			if (draft && draft.messageId === row.dataset.messageId) marks.push({ ...draft.anchor, id: 'draft', kind: 'pending' } as typeof marks[number]);
			for (const comment of marks) {
				if ((body.textContent ?? '').slice(comment.from_off, comment.to_off) !== comment.quote) continue;
				const range = document.createRange();
				range.setStart(...point(body, comment.from_off)); range.setEnd(...point(body, comment.to_off));
				keepingSelection(body, () => wrapRange(range, { from: comment.from_off, to: comment.to_off, id: comment.id, kind: comment.kind }));
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
		observer = new MutationObserver(() => { if (!painting) schedule(); });
		const root = messageRoot();
		if (root) observer.observe(root, { childList: true, subtree: true, characterData: true });
		return () => { observer?.disconnect(); window.removeEventListener('resize', update); document.removeEventListener('selectionchange', readSelection); };
	});
	export async function edit(id: string) {
		const comment = comments.find(item => item.id === id);
		if (!comment) return;
		editingId = id; await tick(); schedule();
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		document.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(comment.messageId)}"] .cm-comment-focused`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
	async function submit(note: string) {
		if (draft) onChange([...comments, { id: crypto.randomUUID(), messageId: draft.messageId, seq: draft.seq, ...draft.anchor, note }]);
		else if (editing) onChange(comments.map(item => item.id === editing.id ? { ...item, note } : item));
		close();
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
			<AdaptiveDrawer ariaLabel="Chat comment" heading={editing ? 'Edit comment' : 'New comment'} followKeyboard onClose={close}>
				<CommentComposer quote={editing?.quote ?? draft?.anchor.quote ?? ''} body={editing?.note ?? ''} submitLabel={editing ? 'Save' : 'Comment'} submittingLabel={editing ? 'Saving…' : 'Posting…'} onSubmit={submit} onCancel={close} />
			</AdaptiveDrawer>
		{:else}
			<Popover {anchor} bounds={messageRoot()} placement="bottom" open={true} onClose={close}>
				<CommentComposer quote={editing?.quote ?? draft?.anchor.quote ?? ''} body={editing?.note ?? ''} submitLabel={editing ? 'Save' : 'Comment'} submittingLabel={editing ? 'Saving…' : 'Posting…'} onSubmit={submit} onCancel={close} />
			</Popover>
		{/if}
	{/key}
{/if}
