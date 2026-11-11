import { tick } from 'svelte';
import { messages } from '$lib/stores/threads';
import { hydrateFetchedMessages, mergeFetchedHistoryRows } from '$lib/messageHistory';
import type { MessagePage, PagerPort } from '$lib/messagePager';
import { apiFetch } from '$lib/api/request';

/**
 * The transcript pager's port: how an older page of the thread on screen is fetched, hydrated into the
 * message store, and prepended under the reader's place. It is one contract (`PagerPort`) and holds none of
 * the panel's own state, so it lives here and `ChatPanel` hands it what it reads.
 */
export function createTranscriptPagerPort(inputs: {
	/** The signed-in token, read when a page is fetched so a re-login needs no new port. */
	token: () => string | null;
	/** The thread on screen, read per page: a page belongs to whichever chat is open when it starts. */
	threadId: () => string | null;
	pageSize: number;
	/** What the paging query adds beyond the limit and cursor, such as Focus's `view` parameter. */
	querySuffix: () => string;
	/** The transcript the reader is holding their place in. */
	scrollEl: () => HTMLElement | null;
}): PagerPort {
	return {
		async fetchPage(cursor: string): Promise<MessagePage | null> {
			const token = inputs.token(), threadId = inputs.threadId();
			if (!token || !threadId) return null;
			try {
				const res = await apiFetch(`/api/threads/${threadId}/messages?token=${token}&limit=${inputs.pageSize}&before=${cursor}${inputs.querySuffix()}`);
				if (!res.ok) return null;
				const data = await res.json() as MessagePage;
				return { ...data, messages: hydrateFetchedMessages(data.messages ?? []) };
			} catch (e) {
				console.error('loadOlderMessages fetch failed:', e);
				return null;
			}
		},
		prepend(older) { messages.update(current => mergeFetchedHistoryRows(current, older)); },
		getScrollEl: () => inputs.scrollEl(),
		afterRender: () => tick(),
	};
}
