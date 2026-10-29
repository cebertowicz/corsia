import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { chatReplyJump } from '$lib/stores/chatReplyJump';
import type { AttentionDestination } from '$lib/api/attentionQueue';
import type { PagerState } from '$lib/messagePager';
import type { RunLoad, RunStub } from '$lib/focusRuns';

export type JumpPort = {
	pager: () => PagerState;
	runs: () => readonly RunStub[];
	loads: () => Record<string, RunLoad>;
	loadOlder: () => Promise<void>;
	loadRun: (run: RunStub) => Promise<void>;
	element: () => HTMLElement | undefined;
	generation: () => number;
};

type Item = { destination: AttentionDestination; threadId: string; generation: number; version: number };

export function createChatReplyJumper(port: JumpPort) {
	let pending: Item | null = null;
	let version = 0;
	let running = false;
	const current = (item: Item) =>
		item.version === version && item.threadId === get(activeThreadId) && item.generation === port.generation();
	const loadedFor = (item: Item) => get(messages).some(row => row.thread_id === item.threadId && row.id === item.destination.id);
	const stubFor = (item: Item) => port.runs().find(run => run.firstSeq <= item.destination.seq! && item.destination.seq! <= run.lastSeq);
	/** A page or Focus run read the jump needs is in flight or failed; its settling resumes the jump. */
	const waiting = (item: Item) => {
		if (port.pager().loading || port.pager().error) return true;
		const run = loadedFor(item) ? undefined : stubFor(item);
		const state = run && port.loads()[run.lastId];
		return !!(state?.loading || state?.error);
	};

	async function run(item: Item): Promise<boolean> {
		const { destination, threadId } = item;
		const target = { threadId, messageId: destination.id, seq: destination.seq!, cid: destination.cid! };
		const loaded = () => loadedFor(item);
		const stub = () => stubFor(item);
		while (!loaded() && !stub() && port.pager().hasMore && !port.pager().error && !port.pager().loading && current(item)) {
			const before = port.pager().oldestId;
			await port.loadOlder();
			if (port.pager().oldestId === before) break;
		}
		if (!current(item)) return false;
		if (waiting(item)) return true;
		const run = stub();
		while (!loaded() && run && current(item)) {
			if (waiting(item)) return true;
			if (port.loads()[run.lastId]?.hasMore === false) break;
			await port.loadRun(run);
		}
		if (!loaded() || !current(item)) return false;
		chatReplyJump.set(target);
		await tick();
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		if (!current(item)) {
			if (get(chatReplyJump) === target) chatReplyJump.set(null);
			return false;
		}
		const element = port.element();
		const row = element?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(target.messageId)}"]`);
		const card = row?.querySelector<HTMLElement>(`[data-chat-comment="${CSS.escape(target.cid)}"]`);
		chatReplyJump.set(null);
		if (!card) return false;
		const mark = [...(row?.querySelectorAll<HTMLElement>('[data-comment-ids]') ?? [])]
			.find(item => item.dataset.commentIds?.split(',').includes(card.dataset.commentUuid ?? ''));
		const focus = mark ?? card;
		const priorTabIndex = focus.getAttribute('tabindex');
		focus.tabIndex = -1;
		focus.scrollIntoView({ block: 'center' });
		focus.focus({ preventScroll: true });
		card.classList.add('chat-reply-landed');
		mark?.classList.add('chat-reply-mark-landed');
		const clear = () => {
			card.classList.remove('chat-reply-landed');
			mark?.classList.remove('chat-reply-mark-landed');
			if (priorTabIndex === null) focus.removeAttribute('tabindex'); else focus.setAttribute('tabindex', priorTabIndex);
		};
		if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
			element?.addEventListener('scroll', clear, { once: true });
			card.addEventListener('pointerdown', clear, { once: true });
		} else window.setTimeout(clear, 2000);
		return false;
	}

	function resume(): void {
		if (!pending || running) return;
		if (!current(pending)) { pending = null; return; }
		if (waiting(pending)) return;
		const item = pending;
		running = true;
		void run(item).then((deferred) => {
			if (pending === item && !deferred) pending = null;
		}).finally(() => {
			running = false;
			if (pending && !waiting(pending)) resume();
		});
	}

	return {
		jump(destination: AttentionDestination) {
			const threadId = get(activeThreadId);
			if (!threadId || !destination.cid || typeof destination.seq !== 'number') return;
			pending = { destination, threadId, generation: port.generation(), version: ++version };
			resume();
		},
		resume,
		cancel() { ++version; pending = null; },
	};
}
