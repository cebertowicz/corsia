import { anchorPrepend, contentEnd, type Scroller } from '$lib/actions/holdTail';

export type TurnSummary = {
	started_at: string | null;
	ended_at: string | null;
	status: string;
	activity_count: number;
	cause?: string | null;
};

export interface MessagePage {
	messages: any[];
	has_more: boolean;
	oldest_id: string | null;
	turn_summaries?: Record<string, TurnSummary>;
	chat_reply_items?: import('./api/attentionQueue').CommentReplyItem[];
	/** A Focus page: one stub per collapsed run (see `focusRuns`). */
	runs?: unknown;
}

export interface PagerState {
	hasMore: boolean;
	oldestId: string | null;
	loading: boolean;
	error: boolean;
}

export type ScrollAnchor = Scroller;

export const PAGER_ABORT = Symbol('pager-abort');

export interface PagerPort {
	fetchPage: (cursor: string) => Promise<MessagePage | null | typeof PAGER_ABORT>;
	prepend: (older: any[]) => void;
	getScrollEl: () => ScrollAnchor | null;
	afterRender: () => Promise<void>;
}

/** One read at a time: starting the next aborts the one it supersedes. */
export function supersedingRead(): { start: () => AbortSignal; abort: () => void } {
	let current: AbortController | null = null;
	return {
		start() {
			current?.abort();
			current = new AbortController();
			return current.signal;
		},
		abort: () => current?.abort(),
	};
}

export const LOAD_OLDER_TRIGGER_PX = 200;
/** A Focus-mode request may inspect at most 1,000 messages (20 API pages). */
export const MAX_PAGER_CHAIN = 20;

export function createPager(initial?: Partial<PagerState>): PagerState {
	return {
		hasMore: initial?.hasMore ?? false,
		oldestId: initial?.oldestId ?? null,
		loading: false,
		error: false,
	};
}

export function adoptFirstPage(state: PagerState, page: MessagePage): void {
	state.hasMore = page.has_more ?? false;
	state.oldestId = page.oldest_id ?? null;
	state.loading = false;
	state.error = false;
}

export function canLoadOlder(state: Pick<PagerState, 'hasMore' | 'loading' | 'error'>): boolean {
	return state.hasMore && !state.loading && !state.error;
}

/** The content does not fill the view by more than the trigger band, so the top is already in reach and no scroll can ask for older history. */
export function contentFitsView(el: Scroller): boolean {
	return contentEnd(el) - el.clientHeight <= LOAD_OLDER_TRIGGER_PX;
}

export function shouldLoadOlder(state: Pick<PagerState, 'hasMore' | 'loading' | 'error'>, scrollTop: number): boolean {
	return scrollTop < LOAD_OLDER_TRIGGER_PX && canLoadOlder(state);
}

export function requestOlderForVisibleEntry(
	state: Pick<PagerState, 'hasMore' | 'loading' | 'error'>,
	request: () => void,
): boolean {
	if (!canLoadOlder(state)) return false;
	request();
	return true;
}

export function requestOlderIfTop(
	state: Pick<PagerState, 'hasMore' | 'loading' | 'error'>,
	scrollTop: number,
	request: () => void,
): boolean {
	if (!shouldLoadOlder(state, scrollTop)) return false;
	request();
	return true;
}

export async function loadOlder(
	state: PagerState,
	port: PagerPort,
	maxChain: number | undefined = undefined,
	needsMore?: () => boolean,
): Promise<number> {
	const chainLimit = maxChain ?? (needsMore ? MAX_PAGER_CHAIN : 8);
	if (state.loading || !state.hasMore || !state.oldestId) return 0;
	if (chainLimit <= 0) return 0;

	state.loading = true;
	state.error = false;
	let prepended = 0;
	try {
		const page = await port.fetchPage(state.oldestId);
		if (page === PAGER_ABORT) return 0;
		if (!page) {
			state.error = true;
			return 0;
		}

		const older = page.messages ?? [];
		if (older.length === 0) {
			state.hasMore = page.has_more ?? false;
			if (page.oldest_id) state.oldestId = page.oldest_id;
		} else {
			const el = port.getScrollEl();
			const keepPlace = el ? anchorPrepend(el) : null;

			port.prepend(older);
			prepended = older.length;
			state.hasMore = page.has_more ?? false;
			state.oldestId = page.oldest_id ?? null;

			await port.afterRender();
			if (port.getScrollEl()) keepPlace?.();
		}
	} catch (e) {
		console.error('loadOlderMessages failed:', e);
		state.error = true;
		return prepended;
	} finally {
		state.loading = false;
	}

	const el = port.getScrollEl();
	if (el && state.hasMore && (shouldLoadOlder(state, el.scrollTop) || needsMore?.())) {
		prepended += await loadOlder(state, port, chainLimit - 1, needsMore);
	}
	return prepended;
}

export function makeGuardedPort(
	getGen: () => number,
	gen: number,
	base: PagerPort,
): PagerPort {
	return {
		async fetchPage(cursor) {
			if (getGen() !== gen) return PAGER_ABORT;
			const result = await base.fetchPage(cursor);
			if (getGen() !== gen) return PAGER_ABORT;
			return result;
		},
		prepend(older) {
			if (getGen() !== gen) return;
			base.prepend(older);
		},
		getScrollEl: () => (getGen() !== gen ? null : base.getScrollEl()),
		afterRender: () => base.afterRender(),
	};
}
