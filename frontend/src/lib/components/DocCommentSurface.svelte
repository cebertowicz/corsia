<script lang="ts">
	import { tick, type Snippet } from 'svelte';
	import { loadMentionRoster } from './comments/mentionRoster.svelte';
	import { createMentionSelection, type MentionOccurrence } from './comments/mentionSelection';
	import { keptThreads } from '$lib/docKeptThreads.svelte';
	import { get } from 'svelte/store';
	import { Button } from './ui';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { peerFade } from '$lib/popoverMotion';
	import type { CommentLoad } from '$lib/stores/docComments';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover, { anchorRect } from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import CommentList from './comments/CommentList.svelte';
	import SuggestionStatus from './comments/SuggestionStatus.svelte';
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import DocCommentImageHead from './DocCommentImageHead.svelte';
	import DocMarginStack from './DocMarginStack.svelte';
	import DocCommentDrawerHeading from './DocCommentDrawerHeading.svelte';
	import { holdReadingLine } from '$lib/docMarginHold.svelte';
	import { createResolvedDrawer } from '$lib/docResolvedDrawer.svelte';
	import { createSuggestionDecisions } from '$lib/docSuggestionDecisions.svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import { retainUnreadList } from '$lib/docUnreadList.svelte';
	import { byCommentNumber, capSheetBelowAnchor, clickedOpener, drawerKeys, editorTop, fitHalfSheet, imageDrawerMotion, observeCanvasWidth, observeSheetTyping, onSheetMoved, openerFocus, presence, scrollDocumentForCard, scrollListToCard } from '$lib/docCommentDrawer';
	import { focusStillOurs } from '$lib/actions/focusTrap';
	import { restingTop, trackOverlaySheet } from '$lib/cm/visible-region';
	import { createCellLabels, type WidgetLabelReader } from '$lib/docCommentLabels';
	import { createCommentSheet } from '$lib/docCommentSheet.svelte';
	import { marginOut } from '$lib/docCommentDrawerMotion';
	import { marginOnCanvas } from '$lib/docMarginCanvas';
	import { imageGroupKey, onItsImage, type ImageCommentSurface, type DocImageComments } from '$lib/docImageComments';
	import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import { paneOverlay } from '$lib/stores/artifactCanvas';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import { hasRoomForMargin, type CardMode, type CommentAnchor, type CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
	let { state: surfaceState, anchor, anchorAtClick = false, bounds = null, narrow, phone = null, onRelease, comments, widgetLabel, focusedId, focusedIds = null, draft, draftReason = '', onDraftBody, onCancelDraft, onClose, restoreFocus, onFocus, onSubmit, onReply, onResolve, onAgree, onViewed, target, docPath = null, artifactId, readOnly = false, onSuggestionBusy, onSuggestionAccepted, onDocumentChanged, load, onRetry, image = null, images = null, covered = false, readStampSuppressed = false, onDrawerShown, onSheetModalChange, onRevealDraft, onThreadMissing, reviewBar, reviewVisible = false, quiet = false, draftOverColumn = false, resolvedCount = $bindable(0), resolvedOpen = $bindable(false) }: {
		state: CommentSurfaceState;
		reviewBar?: Snippet<[boolean]>;
		reviewVisible?: boolean;
		onThreadMissing?: (remaining: string[]) => void;
		/** The column opened on its own at load, so opening it does not hide the sidebar or the Workspace panel. */
		quiet?: boolean;
		anchor: CommentAnchor | null;
		/** The anchor is where a click opened the card: the card opens just under it, not beside the text. */
		anchorAtClick?: boolean;
		/** The canvas the card stays inside. */
		bounds?: HTMLElement | null;
		/** The pane is a drawer over the canvas: the margin column has no room. */
		narrow: boolean;
		/** A phone: a card or the composer is the sheet. Anywhere else with no margin they are a popover at the click; a surface that does not say follows `narrow`. */
		phone?: boolean | null;
		/** With the margin column up, lets the picked card go; answers whether one was picked. */
		onRelease?: () => boolean;
		comments: DocComment[];
		/** Reads a table comment's label from the editor's own syntax tree. */
		widgetLabel?: WidgetLabelReader;
		focusedId: string | null;
		focusedIds?: string[] | null;
		/** `label` names what a non-text comment is on, in place of the quote. */
		draft: { text: string; body?: string; label?: string; mentionSelection?: ReturnType<typeof createMentionSelection> } | null;
		draftReason?: string;
		onDraftBody?: (text: string) => void;
		onCancelDraft?: () => void;
		onClose: () => void;
		restoreFocus: () => void;
		onFocus: (id: string, placement?: 'top') => void;
		onSubmit: (text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
		onReply: (id: string, text: string, taggedAgentIds: string[], mentions: MentionOccurrence[]) => Promise<void>;
		/** With `wait` the comment changes only once the request is answered; the promise says whether it was taken. */
		onResolve: (id: string, resolved: boolean, wait?: boolean) => void | Promise<boolean>;
		onAgree: (id: string, agreed: boolean) => void;
		onViewed?: (id: string, replyId: string) => Promise<void>;
		target: DocViewerTarget | null;
		/** The commented document's path for comment links, used only when there is no `target` (the image lightbox). */
		docPath?: string | null;
		artifactId: string | null;
		readOnly?: boolean;
		/** A decision is in flight (`busy`): the editor holds still, and while it is an Accept the suggestion `id` is marked busy. */
		onSuggestionBusy?: (busy: boolean, id?: string) => void;
		onSuggestionAccepted?: (text: string) => void;
		/** The server applied an accepted edit but could not record the decision: read the shown document again. */
		onDocumentChanged?: () => void;
		load?: CommentLoad;
		onRetry?: () => void;
		image?: ImageCommentSurface | null;
		images?: DocImageComments | null;
		/** An image opened from the document covers it, the drawer with it: the drawer stays open beneath, and gets its focus back after. */
		covered?: boolean;
		readStampSuppressed?: boolean;
		/** Whether the comment list's drawer is on screen, in either form: true from its mount until its outro has ended. */
		onDrawerShown?: (shown: boolean) => void;
		/** The sheet controller's modal presence, including its closing scrim. */
		onSheetModalChange?: (open: boolean) => void;
		/** Asks the editor to keep the draft's text clear of the sheet, now and whenever the sheet moves. */
		onRevealDraft?: (opening: boolean) => void;
		/** Resolved threads at every width (zero on an image's drawer), for the header control that opens their drawer. */
		resolvedCount?: number;
		/** A new comment is being written over the margin column: the column stays, its cards where they were, with the composer beside the text. */
		draftOverColumn?: boolean;
		/** The resolved drawer is open: set by that control only. */
		resolvedOpen?: boolean;
	} = $props();
	loadMentionRoster(() => ({ artifactId, threadId: target?.threadId ?? null }));
	const asDoc = (comment: unknown) => comment as DocComment;
	const cellLabel = createCellLabels(() => widgetLabel);
	const listDrawerShown = presence((shown) => onDrawerShown?.(shown));
	function movePinInDocument(id: string) {
		const comment = comments.find((item) => item.id === id);
		if (comment && onItsImage(comment)) images?.onOpen(comment, { id, move: true });
	}
	/** The image drawer's own Filter. A document shows its open threads (`view`); its resolved ones are in the resolved drawer. */
	let filter = $state<'all' | 'open' | 'resolved'>('all');
	let filterOpen = $state(false);
	let drawer = $state<HTMLElement>();
	let canvasWidth = $state(Number.POSITIVE_INFINITY);
	$effect(() => observeCanvasWidth(bounds, (width) => canvasWidth = width));
	// The margin column shows only when the text beside it keeps at least 45 characters per line. `hasRoomForMargin` is the canvas-width rule where that holds (the 308 px column and the canvas's padding leave 314 px of text, 45 characters of the editor font); below it, today's sheet. Image viewers already decide whether their canvas is a sheet.
	const roomForMargin = $derived(hasRoomForMargin(canvasWidth, $paneOverlay));
	const sheetDrawer = $derived(narrow || (!image && !roomForMargin));
	/** How a card opened from the text shows: the phone sheet; else the margin column on any desktop canvas with room, cards beside their anchors; else a popover over the text. The editor reads it, so only a press opens a card over the text. */
	const columnFits = $derived(!narrow && !image && roomForMargin); // the one margin rule: the card mode and the column both read it
	export function cardMode(): CardMode { return (phone ?? narrow) ? 'sheet' : columnFits ? 'margin' : 'popover'; }
	const margin = $derived(columnFits && (surfaceState?.kind === 'list' || surfaceState?.kind === 'thread' || (surfaceState?.kind === 'draft' && draftOverColumn)));
	/** Whether the margin column is up: the editor's room beside the text follows this, never the leaving column's transition. */
	export function marginShown(): boolean { return margin; }
	const selectedId = $derived(surfaceState?.kind === 'thread' ? surfaceState.id : focusedId);
	// A thread whose text is gone keeps its card in the column at the nearest line, labelled "This text isn’t in the document" with a dashed edge (CommentThread). A review conversation never had text: its card, labelled "Review comment", sits after the last card that has a line.
	const resolvedDrawer = createResolvedDrawer({ enabled: () => !image, resolved: () => resolvedList, open: () => resolvedOpen, setOpen: (open) => resolvedOpen = open, setCount: (count) => resolvedCount = count, entry: () => bounds?.querySelector<HTMLElement>('[data-resolved-entry]') ?? null });
	holdReadingLine({ margin: () => margin, bounds: () => bounds, selectedId: () => selectedId });
	let detent = $state<'full' | 'half'>('full');
	const draftTags = createMentionSelection();
	const replyTags = new Map<string, ReturnType<typeof createMentionSelection>>();
	$effect(() => { if (!draft) draftTags.clear(); });
	let replyDrafts = $state<Record<string, string>>({});
	let replySelections = $state<Record<string, [number, number]>>({});
	// A thread someone else resolves while a reply to it is being typed stays in the Open list, as a resolved card, until that reply is sent or cleared.
	/** What the list holds. A document's margin column and its sheet hold the open threads; the resolved ones are only in the resolved drawer the header opens; an image's drawer has its own Filter. */
	const view = $derived(image ? filter : 'open');
	const held = keptThreads({ comments: () => comments, drafts: () => replyDrafts, leavesOnRelease: () => view === 'open', reduced: () => get(prefersReducedMotion), crossfaded: () => get(prefersReducedMotion) && sheetDrawer && surfaceState?.kind === 'list' });
	const unreadListed = retainUnreadList(() => surfaceState?.kind === 'list', () => comments);
	const column = $derived(bounds?.closest<HTMLElement>('.leaf-body') ?? bounds);
	const openCount = $derived(comments.filter((comment) => !comment.resolved).length);
	// Open counts threads that are open: the resolved card kept for an unsent reply says why it is still listed, and the document's chip agrees.
	// A failed load with nothing known claims no number: a zero there would say the document has no discussions.
	const headerCount = $derived((load?.state === 'error' || load?.state === 'loading') && comments.length === 0 ? '' : view === 'open' ? `${openCount} open` : view === 'resolved' ? `${comments.length - openCount} resolved` : `${comments.length} ${comments.length === 1 ? 'thread' : 'threads'}`);
	const sheet = createCommentSheet({
		state: () => surfaceState,
		draft: () => draft,
		sheetDrawer: () => sheetDrawer,
		narrow: () => cardMode() === 'sheet',
		onClose: () => onClose(),
		onCancelDraft: () => (onCancelDraft ?? onClose)(),
	});
	// The sheet owns this lifecycle; the viewer only consumes its signal to avoid showing another control through its scrim.
	$effect(() => {
		onSheetModalChange?.(sheet.modalOpen);
	});
	$effect(() => () => onSheetModalChange?.(false));
	// The editor keeps its text above whichever of these sheets is up: it reads where it rests from `trackOverlaySheet`.
	let listSheet = $state<HTMLElement | null>(null);
	let draftSheet = $state<HTMLElement | null>(null);
	/** The resting edge, even while the sheet is entering, for the image viewer's visible area. */
	export function sheetTop(): number | null {
		const panel = sheet.state?.kind === 'list' && sheetDrawer ? listSheet : draftSheet;
		return sheet.open && panel ? restingTop(panel) : null;
	}
	let typing = $state(false);
	$effect(() => observeSheetTyping(listSheet ?? draftSheet, (value) => typing = value));
	// A half-height sheet grows to fit its open card, leaving the document's top edge and a few lines of text above it.
	$effect(() => detent === 'half' && sheetDrawer ? fitHalfSheet(listSheet, () => editorTop(bounds)) : undefined);
	// A sheet opened on one thread (a tap on its highlight) stops short of the text the thread is on, so the comment never covers what it is about.
	$effect(() => cardMode() === 'sheet' && surfaceState?.kind === 'thread' && !(sheet.state?.kind === 'list' && sheetDrawer) && !isImage ? capSheetBelowAnchor(draftSheet, () => { const rect = cardAnchor ? anchorRect(cardAnchor) : null; return rect ? rect.bottom : null; }) : undefined);
	$effect(() => trackOverlaySheet(bounds, () => covered || !sheet.open ? null : sheet.state?.kind === 'list' && sheetDrawer ? listSheet : draftSheet));
	// The draft sheet exists only at phone width; a narrow canvas's composer is a popover, with no sheet to keep the subject above.
	$effect(() => onSheetMoved(surfaceState?.kind === 'draft' ? draftSheet : null, onRevealDraft));
	// A boolean, so effects that branch on it do not re-run each time the image props object is rebuilt (every hover).
	const isImage = $derived(!!image);
	const shouldShowReply = $derived(!isImage && narrow && focusedId !== null);
	const picks = $derived(image?.picks ?? 0);
	let navigationNotice = $state('');
	// What is shown follows `sheet.state`: the surface's own state, or the one a closing sheet still shows through its outro.
	/** The newest resolved first. The server keeps no resolve time, so this is the time the thread last changed (updatedAt), which a resolve sets. */
	const byNewest = (a: DocComment, b: DocComment) => (b.updatedAt ?? b.createdAt ?? '').localeCompare(a.updatedAt ?? a.createdAt ?? '');
	const isResolved = (comment: DocComment) => comment.resolved && !held.kept(comment) && !held.leaving.has(comment.id) && !unreadListed.has(comment);
	const allShown = $derived(view === 'all' ? comments : view === 'resolved' ? comments.filter(isResolved).sort(byNewest) : comments.filter((comment) => !isResolved(comment)));
	/** Every open thread is a card in the margin, a deleted anchor's and a review conversation's included. */
	const pinned = $derived(allShown);
	/** With the column up a new comment is written in it, level with its selection, so the text it is on stays uncovered. */
	const marginDraft = $derived(margin && surfaceState?.kind === 'draft' ? { id: 'draft', anchor: () => anchorRect(anchor), body: content } : null);
	const resolvedList = $derived(comments.filter((comment) => isResolved(comment) && !comment.review).sort(byNewest));
	const displayed = $derived.by(() => {
		const showing = sheet.state;
		if (showing?.kind === 'thread') return comments.filter((comment) => showing.ids ? showing.ids.includes(comment.id) : comment.id === showing.id);
		// An image's comments read in number order, like its tags.
		return image ? [...allShown].sort(byCommentNumber) : allShown;
	});
	const followed = $derived(surfaceState?.kind === 'thread' ? comments.find((item) => item.id === surfaceState.id) : undefined);
	// A card that follows its text closes once its comment is deleted: there is nothing left to show.
	$effect(() => {
		if (surfaceState?.kind !== 'thread' || typeof anchor !== 'function' || load?.state === 'loading') return;
		if (!followed) {
			if (surfaceState.ids && onThreadMissing) onThreadMissing(comments.map((comment) => comment.id));
			else onClose();
		}
	});
	// Where the followed card last sat. A comment that is resolved or has lost its text has no place in the text any more, but its
	// card stays open there: closing it would take the refusal reason and any reply being typed with it.
	let lastRect: { id: string; rect: DOMRect } | null = null;
	const cardAnchor = $derived.by((): CommentAnchor | null => {
		const follow = anchor, id = surfaceState?.kind === 'thread' ? surfaceState.id : null;
		if (typeof follow !== 'function' || id === null) return anchor;
		const placeless = !!followed && (followed.resolved || !!followed.orphaned);
		return () => {
			const rect = placeless && lastRect?.id === id ? lastRect.rect : follow();
			if (rect) lastRect = { id, rect };
			return rect;
		};
	});
	$effect(() => {
		if (surfaceState?.kind !== 'list') return;
		filter = 'all'; filterOpen = false; navigationNotice = ''; detent = 'full';
		void tick().then(() => {
			const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
			// An image's drawer opens at the selected comment instead (the effect below).
			if (list && !(isImage && focusedId)) list.scrollTop = 0;
			// A column opened on load (quiet) leaves focus where it was.
			if (!quiet) drawer?.querySelector<HTMLElement>(focusedIds ? '[data-comment-id].focused .ct-num' : '[data-drawer-filter]')?.focus({ preventScroll: true });
		});
	});
	// An image's selected comment, picked on the image or there when the drawer opens, is scrolled to and marked in it.
	// The drawer opens already at its card, so only the drawer moves; a pick while it is open glides the list.
	// The drawer says what to do; the raw transport error stays in the log.
	$effect(() => { if (load?.state === 'error') console.warn('Could not load comments', load.error); });
	let listShown = false;
	$effect(() => {
		const id = focusedIds ? displayed.find((comment) => focusedIds.includes(comment.id))?.id : focusedId;
		// Picking the selected tag again scrolls back to its card.
		void picks;
		const shown = isImage && surfaceState?.kind === 'list';
		const opening = shown && !listShown;
		listShown = shown;
		if (!shown || !id) return;
		void tick().then(() => scrollListToCard(drawer, id, opening, !!image?.candidateOrder));
	});
	/**
	 * An image's drawer slides with the grid track its viewer animates, so it stays mounted while the track closes; with
	 * reduced motion it crossfades instead. A document's side drawer (a canvas wide enough for a column) appears and leaves
	 * at once; its sheet form uses AdaptiveDrawer's open/onClosed lifecycle.
	 */
	function drawerMotion(node: Element, options: { leaving: boolean }) { return imageDrawerMotion(node, options, isImage); }
	function closeDrawer() { const modal = sheet.modalOpen; onClose(); if (!modal) restoreFocus(); }
	// In the margin the first Escape lets the picked card go (focus back to where it came from); the next closes the column.
	function escapeColumn() { if (margin && onRelease?.()) restoreFocus(); else closeDrawer(); }
	// Resolving a decided suggestion ends its conversation, and its edit is already settled in the text, so its card closes with it (DRAFT-comment-fixes-3to6, Option A step 5; in the margin the card leaves the column by itself and the column stays up), but only once the request is answered: on a refusal the card stays as it was, with focus where it was, and says so through this opening's card errors, which reset with the decisions' when the surface reopens.
	function resolveThread(id: string, resolved: boolean) {
		if (!(resolved && (margin || (surfaceState?.kind === 'thread' && surfaceState.id === id)) && comments.find((item) => item.id === id)?.decision)) { void onResolve(id, resolved); return; }
		void decisions.resolveDecided(id, () => onResolve(id, true, true)).then((taken) => {
			if (!taken || margin || surfaceState?.kind !== 'thread' || surfaceState.id !== id) return;
			onClose(); if (cardMode() !== 'sheet') restoreFocus();
		});
	}
	function finishSheetClose() {
		const returnFocus = focusStillOurs(listSheet ?? draftSheet, document.activeElement);
		sheet.finishClose();
		if (!sheet.open && returnFocus) void tick().then(() => restoreFocus());
	}
	/** The card or thumbnail last clicked in the drawer: what an image covering the document was opened from. */
	let opener: Element | null = null;
	// Covered by the image a card opened, the drawer keeps its scroll; uncovered, that card has focus again.
	$effect.pre(() => {
		if (!covered) return;
		const back = openerFocus(opener, document.activeElement);
		return () => void tick().then(() => back?.isConnected && back.focus({ preventScroll: true }));
	});
	function navigate(id: string) {
		filterOpen = false;
		const comment = comments.find((item) => item.id === id);
		navigationNotice = comment?.orphaned || comment?.review ? 'This comment has no text to jump to. Its quote and discussion remain here.' : '';
		const useHalf = sheetDrawer && !comment?.regionNumber && !comment?.orphaned && !comment?.review;
		if (useHalf) { detent = 'half'; onFocus(id, 'top'); } else onFocus(id);
	}
	const decisions = createSuggestionDecisions({
		comments: () => comments, artifactId: () => artifactId, target: () => target, open: () => surfaceState !== null,
		onSuggestionBusy: (...args) => onSuggestionBusy?.(...args), onSuggestionAccepted: (text) => onSuggestionAccepted?.(text), onDocumentChanged: () => onDocumentChanged?.(),
	});
	// A refused accept (kind `stale`) goes to `suggestionRefusals` instead, which also shows its card as orphaned.
	const cardErrors = $derived({
		...(artifactId ? $suggestionRefusals[artifactId] : undefined),
		...Object.fromEntries(Object.entries(decisions.outcome.errors).filter(([, message]) => message)),
	});
</script>

{#snippet imageHead(row: unknown[])}<DocCommentImageHead {row} {images} />{/snippet}
{#snippet threadList(items: DocComment[], reply?: boolean, inMargin?: boolean, underTextChanged?: boolean, flat?: boolean)}
	<CommentList {replyTags} {underTextChanged} {flat} docPath={target?.path ?? docPath} bind:replyDrafts bind:replySelections leavingIds={held.leaving} keptIds={new Set(comments.filter(held.kept).map((comment) => comment.id))} reduced={$prefersReducedMotion} comments={items} {cellLabel} documentOrder={inMargin || sheet.state?.kind === 'list'} {focusedId} {focusedIds} restCards={!!inMargin && !flat} candidateOrder={image?.candidateOrder ?? null} hoveredId={image?.hoveredId ?? null} onHover={image?.onHover} scrollOnFocus={!isImage && !inMargin} focusReply={isImage && sheet.state?.kind === 'thread'} showReply={reply ?? shouldShowReply} keepReplyVisible={(sheetDrawer && sheet.state?.kind === 'list') || !!inMargin} onViewed={covered || readStampSuppressed || !surfaceState ? undefined : onViewed} onFocus={sheet.state?.kind === 'list' && !inMargin ? navigate : onFocus} {onReply} onResolve={resolveThread} {onAgree} onDecision={decisions.decideOne} decisionErrors={cardErrors} tagColor={image?.colorOf ?? (images ? (comment) => images.colorOf(asDoc(comment)) : undefined)} onMovePin={image?.onMovePin ?? (images ? movePinInDocument : undefined)} groupOf={images ? (comment) => imageGroupKey(asDoc(comment), !!images.diagramOf?.(asDoc(comment))) : undefined} diagramOf={(comment) => image?.diagram ?? images?.isDiagram?.(asDoc(comment)) ?? false} groupHead={images ? imageHead : undefined} {readOnly} reopenReachesOwner={load?.state === 'ready' && load.owner != null} />
{/snippet}
{#snippet acceptAllBar()}
	<SuggestionStatus authors={decisions.authorsWithOpenSuggestions} busy={decisions.acceptAllBusy} shownBusy={decisions.outcome.acceptAllShownBusy} skipped={decisions.outcome.skipped} error={decisions.outcome.acceptAllError} acceptAll={decisions.acceptAllFrom} />
{/snippet}
{#snippet content()}
	{#if sheet.state?.kind === 'draft' && sheet.draft}<CommentComposer selectedTags={sheet.draft?.mentionSelection ?? draftTags} quote={sheet.draft.text} label={sheet.draft.label} body={sheet.draft.body ?? ''} disabledReason={draftReason} onBody={onDraftBody} goesTo={load?.state === 'ready' ? load.goesTo : load ? { state: load.state } : undefined} {onRetry} onSubmit={onSubmit} onCancel={sheet.cancelDraft} />
	{:else}
		{#if !readOnly && sheet.state?.kind !== 'thread'}{@render acceptAllBar()}{/if}
		<!-- One thread in the popover draws flat: the container is its card. In the phone sheet it is the same boxed card the list sheet draws, under the same heading. -->
		{@render threadList(displayed, undefined, undefined, sheet.state?.kind === 'thread', sheet.state?.kind === 'thread' && displayed.length === 1 && cardMode() !== 'sheet')}
	{/if}
{/snippet}
{#snippet rowHost(above = true)}<div class="rb-sheet-host" class:above hidden={typing}>{#if reviewVisible && reviewBar}{@render reviewBar(true)}{/if}</div>{/snippet}
{#snippet listBody()}
	{#if sheetDrawer}{@render rowHost(false)}{/if}
	<!-- On the canvas the open threads are cards beside their text, with no heading or close around them: the column closes with the comments control. -->
	{#if !margin}<DocCommentDrawerHeading count={headerCount} filtered={!!image} bind:filter bind:filterOpen onClose={closeDrawer} />{/if}
	{#if load?.state === 'loading'}<p class="drawer-status">Loading comments…</p>{/if}
	{#if load?.state === 'error'}<div class="drawer-status drawer-error" role="status"><span>Couldn't load comments. Check the connection and retry.</span><Button size="sm" onclick={onRetry}>Retry</Button></div>{/if}
	{#if navigationNotice}<p class="drawer-status">{navigationNotice}</p>{/if}
	<!-- Accept all belongs to the column too, but takes no room unless there is something to say. -->
	{#if margin && !readOnly && (decisions.authorsWithOpenSuggestions.length || decisions.outcome.skipped.length || decisions.outcome.acceptAllError)}<div class="margin-accept-all">{@render acceptAllBar()}</div>{/if}
	<div class="drawer-comments" class:in-margin={margin}>
		{#if image && image.showThumbnail !== false && displayed.some((comment) => comment.region)}<ImageRegionThumb src={image.src} alt={image.alt} comments={displayed} colorOf={image.colorOf} onOpen={image.onOpen} />{/if}
		<div class="peers">{#key view}<div class="peer" in:peerFade out:peerFade>
		{#if (allShown.length || marginDraft) && margin}<DocMarginStack {bounds} {pinned} {selectedId} composer={marginDraft}>{#snippet card(comment)}{@render threadList([comment], true, true, true)}{/snippet}</DocMarginStack>{:else if displayed.length}{@render content()}{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">{comments.length === 0 ? 'No comments yet.' : view === 'open' ? 'No open comments.' : 'No resolved comments.'}{#if image && filter === 'resolved' && openCount}<button type="button" class="drawer-empty-action" onclick={() => filter = 'open'}>Show open · {openCount}</button>{/if}</p>{/if}
		</div>{/key}</div>
	</div>
{/snippet}
{#snippet cardPopover()}<Popover fill={surfaceState?.kind === 'draft'} onPlace={(card) => { if (surfaceState?.kind === 'thread' && !image) scrollDocumentForCard(card, anchorRect(cardAnchor), bounds); }} anchor={cardAnchor} placement={anchorAtClick ? 'pointer' : 'right'} {bounds} clearAnchor={surfaceState?.kind === 'thread' && !image} avoidRect={image?.imageBounds} initialFocus={image && !readOnly ? '.ct-input' : undefined} takeFocus={!(surfaceState?.kind === 'thread' && surfaceState.fromText)} sideKey={surfaceState?.kind === 'thread' ? surfaceState.ids?.[0] ?? surfaceState.id : anchor} open={true} {onClose} {restoreFocus} keepOpenOnPointer={(target) => !!image && surfaceState?.kind === 'draft' && target instanceof Element && !!bounds?.contains(target) && !!target.closest('[data-image-zoom]')}>{@render content()}</Popover>{/snippet}

{#if surfaceState || sheet.closing}
	{#if margin && surfaceState}
		<section class="canvas-comment-drawer margin" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:marginOnCanvas={{ root: bounds, scroller: '.cm-scroller' }} use:listDrawerShown use:drawerKeys={escapeColumn} onclickcapture={(event) => opener = clickedOpener(event)} out:marginOut|global>{@render listBody()}</section>
	{:else if sheet.state?.kind === 'list' && sheetDrawer}
		<AdaptiveDrawer bind:panel={listSheet} ariaLabel="Document comments" initialFocusSelector={focusedIds ? '[data-comment-id].focused .ct-num' : '[data-drawer-autofocus]'} headless fill opaque {detent} onDetent={(next) => detent = next} followKeyboard={narrow} within={column} {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose}><section class="canvas-comment-drawer sheet" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)}>{@render listBody()}</section></AdaptiveDrawer>
	{:else if surfaceState?.kind === 'list'}
		<section class="canvas-comment-drawer" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)} in:drawerMotion|global={{ leaving: false }} out:drawerMotion|global={{ leaving: true }}>{@render listBody()}</section>
	{:else if cardMode() === 'sheet' && sheet.state?.kind === 'thread' && !isImage}<AdaptiveDrawer bind:panel={draftSheet} ariaLabel="Document comments" headless fill opaque followKeyboard within={column} {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose} above={rowHost}><section class="canvas-comment-drawer sheet"><DocCommentDrawerHeading count={headerCount} filtered={false} bind:filter bind:filterOpen onClose={sheet.close} /><div class="drawer-comments">{@render content()}</div></section></AdaptiveDrawer>
	{:else if cardMode() === 'sheet' && sheet.state}<AdaptiveDrawer fill={sheet.state.kind === 'draft'} bind:panel={draftSheet} ariaLabel="Document comments" heading={sheet.state.kind === 'draft' ? 'New comment' : null} doneLabel="Close" opaque followKeyboard {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose} above={rowHost}>{@render content()}</AdaptiveDrawer>
	{:else if surfaceState}{@render cardPopover()}{/if}
{/if}
<!-- Its own drawer over the document pane (`within` the leaf body, as the comment sheet is), at every width: never inside the margin column or the sheet. -->
{#if resolvedDrawer.mounted}<AdaptiveDrawer ariaLabel="Resolved comments" heading="Resolved" headingMeta={String(resolvedList.length)} doneLabel="Close" opaque within={column} {covered} open={resolvedOpen} onClose={resolvedDrawer.close} onClosed={resolvedDrawer.closed}><div class="resolved-drawer" use:drawerKeys={resolvedDrawer.close}>{@render threadList(resolvedList, true, true, false, true)}</div></AdaptiveDrawer>{/if}

<style>
	/* Keep the review controls in the canvas container when the phone sheet is open. */
	.rb-sheet-host { container: doc-canvas / inline-size; padding: var(--space-2) 0 0; flex: none; overflow: hidden; scrollbar-gutter: stable; }
	.rb-sheet-host.above { margin: 0 2px 0 -4px; padding-top: 0; }
	.rb-sheet-host:empty, .rb-sheet-host[hidden] { display: none; }
	.canvas-comment-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; padding: var(--space-1) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
	/* The column is drawn in the editor's scroller (`marginOnCanvas`), in the room the editor keeps right of its text (DocViewer.css), with no box, rule or heading of its own; until it is there it is not drawn. */
	.canvas-comment-drawer.margin { display: none; position: absolute; z-index: 1; top: 0; right: calc(var(--space-2) + var(--pane-overlay-inset, 0px)); width: calc(var(--margin-column) - var(--space-3) - var(--space-2)); padding: 0; border: 0; }
	.canvas-comment-drawer.margin:global(.on-canvas) { display: flex; }
	/* The resolved drawer's cards share the margin cards' corners. */
	.resolved-drawer { --ct-radius: var(--radius); }
	/* The column grows in from nothing; its content keeps the final width so the reply fields measure at it. */
	.canvas-comment-drawer.margin > :global(*) { min-width: 287px; }
	/* The column never scrolls on its own: its cards are part of the page and overflow it downward, so the scroller reaches them. */
	.drawer-comments.in-margin { position: relative; overflow: visible; scrollbar-gutter: auto; }
	.canvas-comment-drawer.sheet { flex: 1; padding: 0; border-top: 0; }
	@container doc-canvas (min-width: 900px) { .canvas-comment-drawer { border-top: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-3); } }
	/* A filter switch crossfades: the leaving and arriving lists share one grid cell, so neither moves the other. */
	.peers { display: grid; }
	.peer { grid-area: 1 / 1; min-width: 0; }
	.drawer-comments { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
	.drawer-status { color: var(--text-secondary); font-size: var(--text-footnote); margin: 4px 0; }
	/* Retry sits on its own row under the sentence, so it never touches the end of a wrapped line. */
	.drawer-error { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); }
	.drawer-empty-action { margin-left: var(--space-1); padding: 0; border: 0; background: none; color: var(--accent-text); font: inherit; font-weight: var(--fw-semibold); cursor: pointer; }
	.drawer-empty-action:hover { text-decoration: underline; }
	.margin-accept-all { flex: none; padding: 0 0 var(--space-1) 0; }
</style>
