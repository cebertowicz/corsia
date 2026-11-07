<script module lang="ts">
	/** Cancels the pending removal of a canvas's room below the text, per canvas: a column that comes back before it keeps the room. */
	const restoring = new WeakMap<HTMLElement, () => void>();
</script>

<script lang="ts">
	import { untrack, type Snippet } from 'svelte';
	import { get } from 'svelte/store';
	import { GAP, placeCards, revealShift, type MarginItem } from '$lib/docMarginPlacement';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { afterHold } from '$lib/docMarginHold.svelte';
	import type { DocComment } from '$lib/stores/docComments';
	/** The margin column's cards: each open thread beside its text, stacked where a cluster would overlap. */
	let { bounds, pinned, selectedId, behind, card }: { bounds: HTMLElement | null; pinned: DocComment[]; selectedId: string | null; behind: boolean; card: Snippet<[DocComment]> } = $props();
	let stackEl = $state<HTMLElement>();
	let tops = $state<Record<string, number>>({});
	/** The card picked last, so only a new pick (not a re-layout) is brought into view when it grows. */
	let pickedBefore: string | null = null;
	// One scroll: the cards sit in document coordinates inside a clip below the column header and move with the editor's scroller.
	$effect(() => {
		if (!stackEl || !bounds) return;
		const scroller = bounds.querySelector<HTMLElement>('.cm-scroller'), clip = stackEl.closest<HTMLElement>('.drawer-comments');
		if (!scroller || !clip) return;
		restoring.get(bounds)?.();
		restoring.delete(bounds);
		const ids = pinned.map((comment) => comment.id), selected = selectedId;
		// A card just picked grows by its reply field: for a moment after the pick, and while the reader is inside it, its lower end (the Send button) is kept in the clip by scrolling the text, which the cards ride with. The clip itself never scrolls.
		const revealUntil = selected !== null && selected !== pickedBefore ? performance.now() + 800 : 0;
		pickedBefore = selected;
		const reveal = () => {
			const el = selected ? stackEl!.querySelector<HTMLElement>(`[data-margin-card="${selected}"]`) : null;
			if (!el || !(performance.now() < revealUntil || el.contains(document.activeElement))) return;
			const shift = revealShift(el.getBoundingClientRect(), clip.getBoundingClientRect());
			if (shift) scroller.scrollBy?.({ top: shift, behavior: get(prefersReducedMotion) ? 'auto' : 'smooth' });
		};
		let frame = 0, overhang = parseFloat(bounds.style.getPropertyValue('--margin-overhang')) || 0;
		// A card shows for as long as its own placed box reaches into the clip, and goes once that box has scrolled fully above the clip top; where its anchor is does not matter, so a card stacked far below its text stays reachable. One that would start above the clip sits at the clip top, under the header, and the cards below it shift down, so none is ever cut off under the header.
		const follow = () => {
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
			const clipTop = clip.getBoundingClientRect().top, scrolled = scroller.scrollTop;
			const items: MarginItem[] = [], loose: MarginItem[] = [];
			for (const id of ids) {
				const marks = [...bounds!.querySelectorAll(`.cm-comment-line[data-comment-ids*="${id}"], .cm-suggestion-del[data-suggestion-id="${id}"]`)];
				const card = stackEl!.querySelector<HTMLElement>(`[data-margin-card="${id}"]`);
				if (marks.length && card) {
					items.push({ id, y: Math.max(0, marks[0].getBoundingClientRect().top - clipTop + scrolled), h: card.offsetHeight });
				} else if (card) loose.push({ id, y: 0, h: card.offsetHeight });
			}
			// A thread with no line at all (its text and its place both gone) sits after the last card that has one.
			const tail = items.reduce((most, item) => Math.max(most, item.y), 0);
			items.push(...loose.map((item) => ({ ...item, y: tail })));
			items.sort((a, b) => a.y - b.y);
			const next = placeCards(items, selected);
			if (Object.keys(next).length !== Object.keys(tops).length || Object.entries(next).some(([id, top]) => Math.abs((tops[id] ?? 1e9) - top) > 0.5)) tops = next;
			// The text scrolls far enough that the last card sits whole in the clip, as follow() will lay the stack out at that scroll (cards above the clip top are gone, the rest shift down under the header). Nothing is added when the cards already fit.
			const placed = items.map((item) => ({ top: next[item.id] ?? 0, h: item.h })).sort((a, b) => a.top - b.top);
			const lastBottomAt = (at: number) => { let bottom = at, last = -Infinity; for (const card of placed) { if (card.top + card.h <= at) continue; const top = card.h > clip.clientHeight ? card.top : Math.max(card.top, bottom); last = top + card.h; bottom = last + GAP; } return last; };
			let low = 0, high = placed.reduce((most, card) => Math.max(most, card.top + card.h), 0);
			for (let i = 0; i < 24 && high - low > 0.5; i++) { const mid = (low + high) / 2; if (lastBottomAt(mid) <= mid + clip.clientHeight) high = mid; else low = mid; }
			const wanted = Math.max(0, Math.ceil(high) + 1 + Math.round(scroller.clientHeight - (scroller.scrollHeight - overhang)));
			if (wanted !== overhang) { overhang = wanted; bounds!.style.setProperty('--margin-overhang', `${overhang}px`); }
			reveal();
		};
		// Placement is read once per frame only after something that moves a line or a card: the text or its commented lines change, or the
		// scroller, clip or a card resizes. Scrolling moves every line equally, so it only needs follow().
		const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; read(); }); };
		const mutations = new MutationObserver(schedule);
		for (const el of bounds.querySelectorAll('.cm-content, .cm-comment-rail')) mutations.observe(el, { subtree: true, childList: true, attributes: true, characterData: true });
		const sizes = new ResizeObserver(schedule);
		for (const el of [scroller, clip, ...bounds.querySelectorAll('.cm-content'), ...stackEl.querySelectorAll('[data-margin-card]')]) sizes.observe(el);
		scroller.addEventListener('scroll', follow, { passive: true });
		// Untracked: read() both reads and writes `tops`, which would otherwise rerun this setup on every placement it makes.
		untrack(read);
		// The room below the text stays until the reading-line hold has ended (docMarginHold owns when), so a close can still scroll the reader's line
		// back to where it was. It is asked a frame later, once a hold that this same change starts is running.
		const area = bounds;
		return () => {
			cancelAnimationFrame(frame); mutations.disconnect(); sizes.disconnect(); scroller.removeEventListener('scroll', follow);
			let cancelHold = () => {};
			const later = requestAnimationFrame(() => { cancelHold = afterHold(area, () => { restoring.delete(area); area.style.removeProperty('--margin-overhang'); }); });
			restoring.set(area, () => { cancelAnimationFrame(later); cancelHold(); });
		};
	});
</script>

<div class="margin-stack" class:behind bind:this={stackEl}>{#each pinned as comment (comment.id)}<div class="margin-card" class:placed={tops[comment.id] !== undefined} data-margin-card={comment.id} style:top="{tops[comment.id] ?? 0}px">{@render card(comment)}</div>{/each}</div>

<style>
	.margin-stack { position: absolute; top: 0; left: 0; right: 0; will-change: transform; }
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
