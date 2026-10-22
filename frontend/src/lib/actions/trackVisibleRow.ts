import { setVisibleRows } from '$lib/stores/replyContextCapture';

/**
 * Reports which transcript rows are on screen, for the reply context a message
 * carries. One observer serves every row; a row with no seq yet (not delivered)
 * is not tracked until it has one.
 */
const rowSeqs = new WeakMap<Element, number>();
const visible = new Set<Element>();
let observer: IntersectionObserver | null = null;

function report(): void {
	const seqs: number[] = [];
	for (const row of visible) {
		const seq = rowSeqs.get(row);
		if (seq !== undefined) seqs.push(seq);
	}
	setVisibleRows(seqs);
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

export function trackVisibleRow(node: HTMLElement, seq: number | null | undefined) {
	const apply = (value: number | null | undefined) => {
		const watcher = sharedObserver();
		if (typeof value !== 'number') {
			rowSeqs.delete(node);
			watcher?.unobserve(node);
			if (visible.delete(node)) report();
			return;
		}
		rowSeqs.set(node, value);
		watcher?.observe(node);
		if (visible.has(node)) report();
	};
	apply(seq);
	return {
		update: apply,
		destroy() {
			observer?.unobserve(node);
			rowSeqs.delete(node);
			if (visible.delete(node)) report();
		},
	};
}
