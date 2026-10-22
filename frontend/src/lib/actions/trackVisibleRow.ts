import { setVisibleRows } from '$lib/stores/replyContextCapture';

/**
 * Reports which transcript rows are on screen, for the reply context a message
 * carries. One observer serves every row; a row with no seq yet (not delivered)
 * is not tracked until it has one. Each row reports the thread it belongs to.
 */
const rowSeqs = new WeakMap<Element, { seq: number; threadId: string | null }>();
const visible = new Set<Element>();
let observer: IntersectionObserver | null = null;

function report(): void {
	const rows: Array<{ threadId: string; seq: number }> = [];
	for (const node of visible) {
		const row = rowSeqs.get(node);
		if (row?.threadId) rows.push({ threadId: row.threadId, seq: row.seq });
	}
	setVisibleRows(rows);
}

function sharedObserver(): IntersectionObserver | null {
	if (typeof IntersectionObserver === 'undefined') return null;
	observer ??= new IntersectionObserver((entries) => {
		for (const entry of entries) {
			if (entry.isIntersecting) visible.add(entry.target);
			else visible.delete(entry.target);
		}
		report();
	});
	return observer;
}

type RowReport = { seq: number | null | undefined; threadId: string | null };

export function trackVisibleRow(node: HTMLElement, row: RowReport) {
	const apply = (value: RowReport) => {
		const watcher = sharedObserver();
		if (typeof value.seq !== 'number') {
			rowSeqs.delete(node);
			watcher?.unobserve(node);
			if (visible.delete(node)) report();
			return;
		}
		rowSeqs.set(node, { seq: value.seq, threadId: value.threadId });
		watcher?.observe(node);
		if (visible.has(node)) report();
	};
	apply(row);
	return {
		update: apply,
		destroy() {
			observer?.unobserve(node);
			rowSeqs.delete(node);
			if (visible.delete(node)) report();
		},
	};
}
