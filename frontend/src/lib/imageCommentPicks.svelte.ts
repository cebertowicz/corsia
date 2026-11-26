// Which comment on an image is picked (DCOL-FR13.3): from its tag, from the drawer, from ambiguous touch candidates, or
// from the document that opened the viewer on it. The image viewer's half of picking; cards and the drawer are the
// document's own comment surface (`ui`).
import { tick } from 'svelte';
import { fromStore } from 'svelte/store';
import type { createCommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
import type { ImageRegion } from '$lib/imageRegions';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { pickedComments } from '$lib/stores/docComments';

type CommentSurface = ReturnType<typeof createCommentSurfaceState>;

/** The image viewer's picking over its comment surface. `clearDraft` closes a box being drawn, as any pick does. */
export function createImagePicks(ui: CommentSurface, context: {
	clearDraft: () => void;
	/** Scroll a comment's region and its tag into view at the current zoom. */
	reveal: (id: string, region: ImageRegion, fromList?: boolean) => void;
}) {
	const focused = fromStore(focusedDocComment);
	/** Counts tags picked with the drawer open, so picking the selected one again still brings its card back. */
	let listPicks = $state(0);
	let listCandidates = $state<string[] | null>(null);
	let listCandidateOrder = $state<string[] | null>(null);

	/** The drawer's candidates go: the viewer closed its card or drawer. */
	function clear() {
		listCandidates = null;
		listCandidateOrder = null;
	}
	/**
	 * A tag was picked: its card opens beside its box, and closing it returns
	 * focus to the tag. With the drawer open, the drawer stays and the tag is
	 * selected there instead, so the drawer can bring its card into view. Either way it is the person's pick, which reads it.
	 */
	function pick(id: string, anchor: () => DOMRect | null, opener: HTMLElement) {
		listCandidates = null;
		context.clearDraft();
		if (ui.state?.kind === 'list') { focusedDocComment.set(id); pickedComments.set(new Set([id])); listPicks += 1; return; }
		ui.focusMarker([id], opener, anchor);
	}
	/** The drawer opens with no candidates marked. */
	function openList(opener: HTMLElement) {
		clear();
		ui.openList(opener);
	}
	/** Ambiguous touch or pen: use the existing list and mark all candidates in its number order. */
	function pickCandidates(ids: string[], opener: HTMLElement) {
		context.clearDraft();
		if (ui.state?.kind !== 'list') ui.openList(opener);
		listCandidates = ids;
		listCandidateOrder = ids;
		focusedDocComment.set(ids[0]);
		listPicks += 1;
	}
	/** Open a comment's card beside its box, as a pick would: the viewer was opened on it from the document. */
	function select(id: string, tag: HTMLElement, region: ImageRegion | null | undefined, anchor?: () => DOMRect | null) {
		context.clearDraft();
		ui.focusMarker([id], tag, anchor ?? (() => (tag.isConnected ? tag.getBoundingClientRect() : null)));
		if (region) void tick().then(() => {
			if (ui.state?.kind === 'thread' && ui.state.id === id) context.reveal(id, region);
		});
	}
	/** A card in the drawer was selected: the drawer stays, the tag is picked and scrolled into view. */
	function showFromList(id: string, region: ImageRegion | null | undefined) {
		listCandidates = null;
		focusedDocComment.set(id);
		if (region) context.reveal(id, region, true);
	}

	return {
		clear, pick, openList, pickCandidates, select, showFromList,
		get listPicks() { return listPicks; },
		get listCandidates() { return ui.state?.kind === 'list' ? listCandidates : null; },
		get listCandidateOrder() { return ui.state?.kind === 'list' ? listCandidateOrder : null; },
		get selectedId() {
			if (ui.state?.kind === 'thread') return ui.state.id;
			return ui.state?.kind === 'list' ? focused.current : null;
		},
	};
}
