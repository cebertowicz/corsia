<script module lang="ts">
	/** Cancels the pending removal of a canvas's room below the text, per canvas: a column that comes back before it keeps the room. */
	const restoring = new WeakMap<HTMLElement, () => void>();
</script>

<script lang="ts" generics="T extends { id: string }">
	import { untrack, type Snippet } from 'svelte';
	import { get } from 'svelte/store';
	import { GAP, placeCards, revealShift, type MarginItem } from '$lib/docMarginPlacement';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { afterHold } from '$lib/docMarginHold.svelte';
	import { codeMarginSource, type MarginAnchorSource, type MarginComposer } from '$lib/docMarginSource';
	/**
	 * The margin column's cards: each open thread beside its text, stacked where a cluster would overlap. A composer (a new
	 * comment, or an unsent one being edited) is a card too, level with the text it is written on: it keeps its line as a
	 * picked card does, and the cards around it make room.
	 */
	let { bounds = null, source = codeMarginSource(() => bounds), pinned, selectedId, behind, card, composer = null }: { bounds?: HTMLElement | null; source?: MarginAnchorSource; pinned: T[]; selectedId: string | null; behind: boolean; card: Snippet<[T]>; composer?: MarginComposer | null } = $props();
	/** A composer opens ready to type, as its popover does; the column brings it into view, so focusing it never scrolls. */
	const takeFocus = (node: HTMLElement) => { queueMicrotask(() => node.querySelector<HTMLElement>('textarea, [contenteditable="true"]')?.focus({ preventScroll: true })); };
	let stackEl = $state<HTMLElement>();
	let tops = $state<Record<string, number>>({});
	/** The card picked last, so only a new pick (not a re-layout) is brought into view when it grows. */
	let pickedBefore: string | null = null;
	// One scroll. A clip inside the scroller's own content (a column drawn on the canvas, the document's and the chat's) scrolls with the
	// text natively: the cards sit beside their lines and nothing follows. A clip beside the scroller holds the cards in document
	// coordinates below its header and moves them with the scroller.
	$effect(() => {
		if (!stackEl) return;
		const scroller = source.scroller(), clip = source.clip(stackEl), room = source.room();
		if (!scroller || !clip || !room) return;
		// Read each time: a column is moved into the scroller once it has mounted, after this setup has run.
		const onCanvas = () => scroller.contains(clip);
		const area = room.el;
		restoring.get(area)?.();
		restoring.delete(area);
		const writing = composer;
		const ids = [...pinned.map((comment) => comment.id), ...(writing ? [writing.id] : [])], selected = writing?.id ?? selectedId;
		// A card just picked grows by its reply field: for a moment after the pick, and while the reader is inside it, its lower end (the Send button) is kept in view by scrolling the text, which the cards ride with. The clip itself never scrolls. A column on the canvas scrolls inside the text, so its view is the scroller's, not its own box.
		const revealUntil = selected !== null && selected !== pickedBefore ? performance.now() + 800 : 0;
		pickedBefore = selected;
		const reveal = () => {
			const el = selected ? stackEl!.querySelector<HTMLElement>(`[data-margin-card="${selected}"]`) : null;
			if (!el || !(performance.now() < revealUntil || el.contains(document.activeElement))) return;
			const view = onCanvas() ? source.view?.() ?? scroller.getBoundingClientRect() : clip.getBoundingClientRect();
			const shift = revealShift(el.getBoundingClientRect(), view);
			if (shift) scroller.scrollBy?.({ top: shift, behavior: get(prefersReducedMotion) ? 'auto' : 'smooth' });
		};
		let frame = 0, overhang = parseFloat(area.style.getPropertyValue(room.property)) || 0;
		// A card shows for as long as its own placed box reaches into the clip, and goes once that box has scrolled fully above the clip top; where its anchor is does not matter, so a card stacked far below its text stays reachable. One that would start above the clip sits at the clip top, under the header, and the cards below it shift down, so none is ever cut off under the header.
		const follow = () => {
			if (onCanvas()) {
				// Anything a follow laid out before the move is undone, by this setup or an earlier one: the classes outlive a setup.
				for (const el of [stackEl!, ...stackEl!.querySelectorAll<HTMLElement>('[data-margin-card]')]) { el.style.transform = ''; el.classList.remove('under'); }
				return;
			}
			const scrolled = scroller.scrollTop;
			stackEl!.style.transform = `translateY(${-scrolled}px)`;
			const cards = [...stackEl!.querySelectorAll<HTMLElement>('[data-margin-card]')].map((el) => ({ el, top: el.offsetTop, h: el.offsetHeight, out: el.offsetTop + el.offsetHeight <= scrolled })).sort((a, b) => a.top - b.top);
			const room = clip.clientHeight;
			let bottom = scrolled;
			// The selected card keeps its own line. The cards it pushes up that no longer fit whole in the clip go (as one scrolled out of the clip does), so they never push it back down.
			const selectedAt = selected ? cards.findIndex((card) => card.el.dataset.marginCard === selected) : -1;
			for (const [i, card] of cards.entries()) {
				const gone = card.out || (i < selectedAt && card.top < scrolled);
				card.el.classList.toggle('under', gone);
				if (gone) { card.el.style.transform = ''; continue; }
				// A card taller than the clip is never held at the clip top: that would keep its lower part, and its reply field, out of reach. It scrolls up with its text instead.
				const top = card.h > room ? card.top : Math.max(card.top, bottom);
				card.el.style.transform = top > card.top + 0.5 ? `translateY(${top - card.top}px)` : '';
				bottom = top + card.h + GAP;
			}
		};
		const read = () => {
			follow();
			const clipTop = clip.getBoundingClientRect().top, scrolled = onCanvas() ? 0 : scroller.scrollTop;
			const items: MarginItem[] = [], loose: MarginItem[] = [];
			for (const id of ids) {
				const at = id === writing?.id ? writing.anchor()?.top : source.anchor(id)?.getBoundingClientRect().top;
				const card = stackEl!.querySelector<HTMLElement>(`[data-margin-card="${id}"]`);
				if (at !== undefined && card) {
					items.push({ id, y: Math.max(0, at - clipTop + scrolled), h: card.offsetHeight });
				} else if (card) loose.push({ id, y: 0, h: card.offsetHeight });
			}
			// A thread with no line at all (its text and its place both gone) sits after the last card that has one.
			const tail = items.reduce((most, item) => Math.max(most, item.y), 0);
			items.push(...loose.map((item) => ({ ...item, y: tail })));
			items.sort((a, b) => a.y - b.y);
			const next = placeCards(items, selected);
			if (Object.keys(next).length !== Object.keys(tops).length || Object.entries(next).some(([id, top]) => Math.abs((tops[id] ?? 1e9) - top) > 0.5)) tops = next;
			// The text scrolls far enough that the last card sits whole in the clip, as follow() will lay the stack out at that scroll (cards above the clip top are gone, the rest shift down under the header). Nothing is added when the cards already fit.
			// On the canvas the cards are part of the scrolling content, which already reaches as far as the last one.
			const placed = items.map((item) => ({ top: next[item.id] ?? 0, h: item.h })).sort((a, b) => a.top - b.top);
			const lastBottomAt = (at: number) => { let bottom = at, last = -Infinity; for (const card of placed) { if (card.top + card.h <= at) continue; const top = card.h > clip.clientHeight ? card.top : Math.max(card.top, bottom); last = top + card.h; bottom = last + GAP; } return last; };
			let low = 0, high = placed.reduce((most, card) => Math.max(most, card.top + card.h), 0);
			for (let i = 0; i < 24 && high - low > 0.5; i++) { const mid = (low + high) / 2; if (lastBottomAt(mid) <= mid + clip.clientHeight) high = mid; else low = mid; }
			const wanted = onCanvas() ? 0 : Math.max(0, Math.ceil(high) + 1 + Math.round(scroller.clientHeight - (scroller.scrollHeight - overhang)));
			if (wanted !== overhang) { overhang = wanted; area.style.setProperty(room.property, `${overhang}px`); }
			reveal();
		};
		// Placement is read once per frame only after something that moves a line or a card: the text or its commented lines change, or the
		// scroller, clip or a card resizes. Scrolling moves every line equally, so it only needs follow().
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
		for (const el of [scroller, clip, ...source.resized(), ...stackEl.querySelectorAll('[data-margin-card]')]) size(el);
		scroller.addEventListener('scroll', follow, { passive: true });
		// Untracked: read() both reads and writes `tops`, which would otherwise rerun this setup on every placement it makes.
		untrack(read);
		// The room below the text stays until the reading-line hold has ended (docMarginHold owns when), so a close can still scroll the reader's line
		// back to where it was. It is asked a frame later, once a hold that this same change starts is running.
		return () => {
			cancelAnimationFrame(frame); mutations.disconnect(); sizes.disconnect(); scroller.removeEventListener('scroll', follow);
			let cancelHold = () => {};
			const later = requestAnimationFrame(() => { cancelHold = afterHold(area, () => { restoring.delete(area); area.style.removeProperty(room.property); }); });
			restoring.set(area, () => { cancelAnimationFrame(later); cancelHold(); });
		};
	});
</script>

<div class="margin-stack" class:behind bind:this={stackEl}>{#each pinned as comment (comment.id)}<div class="margin-card" class:placed={tops[comment.id] !== undefined} data-margin-card={comment.id} style:top="{tops[comment.id] ?? 0}px">{@render card(comment)}</div>{/each}{#if composer}<div class="margin-card margin-composer" class:placed={tops[composer.id] !== undefined} data-margin-card={composer.id} style:top="{tops[composer.id] ?? 0}px" use:takeFocus>{@render composer.body()}</div>{/if}</div>

<style>
	/* The cards take the Frosted card role through these tokens, which the thread card reads; their corners are the comment popover's. A negative spread keeps each card's lift below it, not spread sideways over its neighbour or the text. */
	.margin-stack { --ct-bg: var(--glass-thin); --ct-border: var(--border); --ct-radius: var(--radius); --ct-shadow: var(--glass-rim), 0 10px 14px -10px color-mix(in srgb, black 22%, transparent); position: absolute; top: 0; left: 0; right: 0; will-change: transform; }
	/* A composer is drawn as a card, in the same frame. */
	.margin-composer { background: var(--ct-bg); border: 1px solid var(--ct-border); border-radius: var(--ct-radius); box-shadow: var(--ct-shadow); padding: 8px 10px; }
	/* The open list covers the whole column, so the cards under it are not drawn: none can show a corner below its edge. */
	.margin-stack.behind, .margin-stack.behind .margin-card.placed { visibility: hidden; }
	/* The cards go and come back with the box: they fade as it pops, and are hidden (not drawn) only once it has finished. */
	.margin-stack { transition: opacity var(--dur-tight) var(--ease-snap); }
	.margin-stack.behind { opacity: 0; transition: opacity var(--dur-tight) var(--ease-snap), visibility 0s linear var(--dur-tight); }
	.margin-stack.behind .margin-card.placed { transition: opacity var(--dur-tight) var(--ease-material), visibility 0s linear var(--dur-tight); }
	.margin-card { position: absolute; left: 0; right: 0; visibility: hidden; transition: opacity var(--dur-tight) var(--ease-material); }
	.margin-card.placed { visibility: visible; }
	.margin-card:global(.under) { opacity: 0; pointer-events: none; }
</style>
