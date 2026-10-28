import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeThreadId, messages } from '$lib/stores/threads';
import { chatReplyJump } from '$lib/stores/chatReplyJump';
import type { AttentionDestination } from '$lib/api/attentionQueue';
import type { PagerState } from '$lib/messagePager';
import type { RunLoad, RunStub } from '$lib/focusRuns';

type JumpPort = {
	pager: () => PagerState;
	runs: () => readonly RunStub[];
	loads: () => Record<string, RunLoad>;
	loadOlder: () => Promise<void>;
	loadRun: (run: RunStub) => Promise<void>;
	element: () => HTMLElement | undefined;
};

export function createChatReplyJumper(port: JumpPort) {
	return async (destination: AttentionDestination) => {
		const threadId = get(activeThreadId);
		if (!threadId || !destination.cid || typeof destination.seq !== 'number') return;
		const target = { threadId, messageId: destination.id, seq: destination.seq, cid: destination.cid };
		const loaded = () => get(messages).some(row => row.thread_id === threadId && row.id === target.messageId);
		const stub = () => port.runs().find(run => run.firstSeq <= target.seq && target.seq <= run.lastSeq);
		while (!loaded() && !stub() && port.pager().hasMore && !port.pager().error && !port.pager().loading && get(activeThreadId) === threadId) {
			const before = port.pager().oldestId;
			await port.loadOlder();
			if (port.pager().oldestId === before) break;
		}
		const run = stub();
		while (!loaded() && run && get(activeThreadId) === threadId) {
			const state = port.loads()[run.lastId];
			if (state?.loading || state?.error || state?.hasMore === false) break;
			await port.loadRun(run);
		}
		if (!loaded() || get(activeThreadId) !== threadId) return;
		chatReplyJump.set(target);
		await tick();
		await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
		const element = port.element();
		const row = element?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(target.messageId)}"]`);
		const card = row?.querySelector<HTMLElement>(`[data-chat-comment="${CSS.escape(target.cid)}"]`);
		chatReplyJump.set(null);
		if (!card) return;
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
	};
}
