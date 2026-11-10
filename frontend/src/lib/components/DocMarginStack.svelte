<script lang="ts" generics="T extends { id: string }">
	import { untrack, type Snippet } from 'svelte';
	import { get } from 'svelte/store';
	import { placeCards, revealShift, type MarginItem } from '$lib/docMarginPlacement';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { codeMarginSource, type MarginAnchorSource, type MarginComposer } from '$lib/docMarginSource';
	/**
	 * The margin column's cards: each open thread beside its text, stacked where a cluster would overlap. A composer (a new
	 * comment, or an unsent one being edited) is a card too, level with the text it is written on: it keeps its line as a
	 * picked card does, and the cards around it make room.
	 */
	let { bounds = null, source = codeMarginSource(() => bounds), pinned, selectedId, card, composer = null }: { bounds?: HTMLElement | null; source?: MarginAnchorSource; pinned: T[]; selectedId: string | null; card: Snippet<[T]>; composer?: MarginComposer | null } = $props();
	/** A composer opens ready to type, as its popover does; the column brings it into view, so focusing it never scrolls. */
	const takeFocus = (node: HTMLElement) => { queueMicrotask(() => node.querySelector<HTMLElement>('textarea, [contenteditable="true"]')?.focus({ preventScroll: true })); };
	let stackEl = $state<HTMLElement>();
	let tops = $state<Record<string, number>>({});
	/** The card picked last, so only a new pick (not a re-layout) is brought into view when it grows. */
	let pickedBefore: string | null = null;
	// One scroll. The column is drawn inside the scroller's own content (marginOnCanvas, the document's and the chat's), so the
	// cards sit beside their lines and scroll with the text natively, and the content already reaches as far as the last card.
	$effect(() => {
		if (!stackEl) return;
		const scroller = source.scroller(), column = source.column(stackEl);
		if (!scroller || !column) return;
		const writing = composer;
		const ids = [...pinned.map((comment) => comment.id), ...(writing ? [writing.id] : [])], selected = writing?.id ?? selectedId;
		// A card just picked grows by its reply field: for a moment after the pick, and while the reader is inside it, its lower end (the Send button) is kept in view by scrolling the text, which the cards ride with. Its view is the scroller's, where the reader sees the content.
		const revealUntil = selected !== null && selected !== pickedBefore ? performance.now() + 800 : 0;
		pickedBefore = selected;
		const reveal = () => {
			const el = selected ? stackEl!.querySelector<HTMLElement>(`[data-margin-card="${selected}"]`) : null;
			if (!el || !(performance.now() < revealUntil || el.contains(document.activeElement))) return;
			const shift = revealShift(el.getBoundingClientRect(), source.view?.() ?? scroller.getBoundingClientRect());
			if (shift) scroller.scrollBy?.({ top: shift, behavior: get(prefersReducedMotion) ? 'auto' : 'smooth' });
		};
		let frame = 0;
		const read = () => {
			const columnTop = column.getBoundingClientRect().top;
			const items: MarginItem[] = [], loose: MarginItem[] = [];
			for (const id of ids) {
				const at = id === writing?.id ? writing.anchor()?.top : source.anchor(id)?.getBoundingClientRect().top;
				const card = stackEl!.querySelector<HTMLElement>(`[data-margin-card="${id}"]`);
				if (at !== undefined && card) {
					items.push({ id, y: Math.max(0, at - columnTop), h: card.offsetHeight });
				} else if (card) loose.push({ id, y: 0, h: card.offsetHeight });
			}
			// A thread with no line at all (its text and its place both gone) sits after the last card that has one.
			const tail = items.reduce((most, item) => Math.max(most, item.y), 0);
			items.push(...loose.map((item) => ({ ...item, y: tail })));
			items.sort((a, b) => a.y - b.y);
			const next = placeCards(items, selected);
			if (Object.keys(next).length !== Object.keys(tops).length || Object.entries(next).some(([id, top]) => Math.abs((tops[id] ?? 1e9) - top) > 0.5)) tops = next;
			reveal();
		};
		// Placement is read once per frame only after something that moves a line or a card: the text or its commented lines change, or the
		// scroller, column or a card resizes. Scrolling moves every line and card together, so it needs nothing.
		const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; read(); }); };
		const sizes = new ResizeObserver(schedule), sized = new WeakSet<Element>();
		const size = (el: Element) => { if (!sized.has(el)) { sized.add(el); sizes.observe(el); } };
		// A node that comes or goes after setup (a page of older rows, a new message) is watched for resize from then on, or let go.
		const mutations = new MutationObserver((records) => {
			// The cards' own changes (typing in one) move no line; a card that grows is a resize, watched below. A column drawn
			// inside the scroller it watches (the chat's) would otherwise re-place every card on every key.
			records = records.filter((record) => !stackEl!.contains(record.target));
			if (!records.length) return;
			for (const record of records) for (const node of record.removedNodes) if (node instanceof Element && sized.delete(node)) sizes.unobserve(node);
			if (records.some((record) => record.addedNodes.length)) source.resized().forEach(size);
			schedule();
		});
		for (const el of source.moved()) mutations.observe(el, { subtree: true, childList: true, attributes: true, characterData: true });
		for (const el of [scroller, column, ...source.resized(), ...stackEl.querySelectorAll('[data-margin-card]')]) size(el);
		// Untracked: read() both reads and writes `tops`, which would otherwise rerun this setup on every placement it makes.
		untrack(read);
		return () => { cancelAnimationFrame(frame); mutations.disconnect(); sizes.disconnect(); };
	});
</script>

<div class="margin-stack" bind:this={stackEl}>{#each pinned as comment (comment.id)}<div class="margin-card" class:placed={tops[comment.id] !== undefined} data-margin-card={comment.id} style:top="{tops[comment.id] ?? 0}px">{@render card(comment)}</div>{/each}{#if composer}<div class="margin-card margin-composer" class:placed={tops[composer.id] !== undefined} data-margin-card={composer.id} style:top="{tops[composer.id] ?? 0}px" use:takeFocus>{@render composer.body()}</div>{/if}</div>

<style>
	/* The cards take the Frosted card role through these tokens, which the thread card reads; their corners are the comment popover's. A negative spread keeps each card's lift below it, not spread sideways over its neighbour or the text. */
	.margin-stack { --ct-bg: var(--glass-thin); --ct-border: var(--border); --ct-radius: var(--radius); --ct-shadow: var(--glass-rim), 0 10px 14px -10px color-mix(in srgb, black 22%, transparent); position: absolute; top: 0; left: 0; right: 0; }
	/* A composer is drawn as a card, in the same frame. */
	.margin-composer { background: var(--ct-bg); border: 1px solid var(--ct-border); border-radius: var(--ct-radius); box-shadow: var(--ct-shadow); padding: var(--space-2) calc(var(--space-2) + var(--space-1) / 2); }
	.margin-card { position: absolute; left: 0; right: 0; visibility: hidden; }
	.margin-card.placed { visibility: visible; }
</style>
