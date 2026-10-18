import { tick, untrack } from 'svelte';
import {
	FOCUS_REACH,
	WINDOW_OVERSCAN,
	anchorShift,
	indexAt,
	itemOffsets,
	mountedIndexes,
	reachAround,
	segmentsFor,
	visibleRange,
	type Segment,
	type WindowItem,
} from './sidebarWindow';

// Starting guesses until the first rows are measured: 38px row + 2px gap, 28px section label.
const DEFAULT_ROW_HEIGHT = 40;
const DEFAULT_LABEL_HEIGHT = 28;
const FIRST_RENDER_ITEMS = 30;

function scrollParent(el: HTMLElement): HTMLElement | null {
	for (let node = el.parentElement; node; node = node.parentElement) {
		const overflowY = getComputedStyle(node).overflowY;
		if (overflowY === 'auto' || overflowY === 'scroll') return node;
	}
	return null;
}

/**
 * Runtime for the windowed sidebar list. Row heights are measured with a ResizeObserver
 * and cached per item key (a row with a second line is taller than one without);
 * unmeasured items use the average measured height of their kind. When a measurement
 * above the viewport changes, scrollTop is shifted by the same amount so the rows
 * the person is looking at do not move. Browser scroll anchoring is switched off on
 * the scroller by the list, because WebKit has none and the two would otherwise add up.
 */
export class SidebarWindow<T> {
	items = $state.raw<WindowItem<T>[]>([]);
	range = $state({ first: 0, last: FIRST_RENDER_ITEMS - 1 });
	focusKey = $state<string | null>(null);
	private version = $state(0);
	private heights = new Map<string, number>();
	private sums = { row: [0, 0], label: [0, 0] };
	private scroller: HTMLElement | null = null;
	private pane: HTMLElement | null = null;
	private observer: ResizeObserver | null = null;
	private frame = 0;

	private offsets = $derived.by(() => {
		void this.version;
		return itemOffsets(this.items.map((item) => this.heightOf(item)));
	});

	private heightOf(item: WindowItem<T>): number {
		const measured = this.heights.get(item.key);
		if (measured !== undefined) return measured;
		const [sum, count] = this.sums[item.kind];
		if (count > 0) return sum / count;
		return item.kind === 'row' ? DEFAULT_ROW_HEIGHT : DEFAULT_LABEL_HEIGHT;
	}

	/**
	 * Replace the item list (a thread moved, arrived or left). Browser scroll anchoring is off on the scroller, so
	 * keep the first visible item at the same screen offset by hand, then recompute the range. At the very top
	 * nothing is anchored, as in the browser: new items appear above and the list stays at the top.
	 */
	setItems(next: WindowItem<T>[]) {
		untrack(() => {
			const view = this.viewport();
			const before = this.items;
			const oldOffsets = this.offsets;
			const anchor = view && view.top > 0 ? indexAt(oldOffsets, view.top) : -1;
			this.items = next;
			if (anchor < 0) return void tick().then(() => this.update());
			const delta = anchorShift(before.map((i) => i.key), oldOffsets, anchor, next.map((i) => i.key), this.offsets);
			void tick().then(() => {
				if (delta && this.scroller) this.scroller.scrollTop += delta;
				this.update();
			});
		});
	}

	/** Items to render, in order, with gap spacers for the runs between them. `kept` are keys that must stay mounted. */
	segments(kept: (string | null)[]): Segment[] {
		const offsets = this.offsets;
		const keys = new Map(this.items.map((item, i) => [item.key, i]));
		const keptIndexes: number[] = [];
		for (const key of kept) {
			const i = key === null ? undefined : keys.get(key);
			if (i !== undefined) keptIndexes.push(i);
		}
		const focus = this.focusKey === null ? undefined : keys.get(this.focusKey);
		if (focus !== undefined) keptIndexes.push(...reachAround(focus, FOCUS_REACH));
		const mounted = mountedIndexes(this.items.length, this.range, WINDOW_OVERSCAN, keptIndexes);
		return segmentsFor(offsets, mounted);
	}

	/** Bind the list: its pane (where items start) and the nearest scrolling ancestor. Returns teardown. */
	attach(pane: HTMLElement): () => void {
		const scroller = scrollParent(pane);
		if (!scroller) return () => {};
		this.pane = pane;
		this.scroller = scroller;
		const previousAnchoring = scroller.style.overflowAnchor;
		scroller.style.overflowAnchor = 'none';
		const onScroll = () => this.schedule();
		scroller.addEventListener('scroll', onScroll, { passive: true });
		this.observer = new ResizeObserver((entries) => this.onResize(entries));
		this.observer.observe(scroller);
		this.update();
		return () => {
			scroller.removeEventListener('scroll', onScroll);
			scroller.style.overflowAnchor = previousAnchoring;
			this.observer?.disconnect();
			this.observer = null;
			cancelAnimationFrame(this.frame);
			this.pane = null;
			this.scroller = null;
		};
	}

	/** Action for the list pane: `attach` with teardown on destroy. */
	attachPane = (pane: HTMLElement) => ({ destroy: this.attach(pane) });

	/** The focused item (and its neighbours) stay mounted so Tab can reach them. */
	onFocusIn = (e: FocusEvent) => {
		this.focusKey = (e.target as HTMLElement).closest<HTMLElement>('[data-win-key]')?.dataset.winKey ?? null;
	};

	onFocusOut = (e: FocusEvent) => {
		if (!(e.relatedTarget instanceof Node) || !(e.currentTarget as HTMLElement).contains(e.relatedTarget)) {
			this.focusKey = null;
		}
	};

	/** Action for each mounted item wrapper (`data-win-key` carries the item key). */
	measure = (el: HTMLElement) => {
		this.observer?.observe(el);
		return { destroy: () => this.observer?.unobserve(el) };
	};

	private viewport(): { top: number; bottom: number } | null {
		const { scroller, pane } = this;
		if (!scroller || !pane) return null;
		const paneTop = pane.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
		const top = scroller.scrollTop - paneTop;
		return { top, bottom: top + scroller.clientHeight };
	}

	private schedule() {
		if (this.frame) return;
		this.frame = requestAnimationFrame(() => {
			this.frame = 0;
			this.update();
		});
	}

	update() {
		const view = this.viewport();
		if (!view) return;
		const next = visibleRange(this.offsets, view.top, view.bottom);
		if (next.first !== this.range.first || next.last !== this.range.last) this.range = next;
	}

	private onResize(entries: ResizeObserverEntry[]) {
		const view = this.viewport();
		let changed = false;
		const before = this.offsets;
		const anchor = view ? indexAt(before, Math.max(0, view.top)) : 0;
		const anchorTop = before[anchor] ?? 0;
		for (const entry of entries) {
			const key = (entry.target as HTMLElement).dataset.winKey;
			if (key === undefined) continue; // the scroller itself: only the viewport height changed
			const height = Math.round(entry.borderBoxSize?.[0]?.blockSize ?? entry.contentRect.height);
			if (height <= 0 || this.heights.get(key) === height) continue;
			this.heights.set(key, height);
			changed = true;
		}
		if (changed) {
			this.sums = { row: [0, 0], label: [0, 0] };
			for (const item of this.items) {
				const h = this.heights.get(item.key);
				if (h !== undefined) {
					this.sums[item.kind][0] += h;
					this.sums[item.kind][1] += 1;
				}
			}
			this.version++;
			const delta = (this.offsets[anchor] ?? 0) - anchorTop;
			void tick().then(() => {
				if (delta !== 0 && this.scroller) this.scroller.scrollTop += delta;
				this.update();
			});
		} else {
			this.update();
		}
	}
}
