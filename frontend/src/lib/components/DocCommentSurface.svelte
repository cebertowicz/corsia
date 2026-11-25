<script lang="ts">
	import { tick } from 'svelte';
	import { loadMentionRoster } from './comments/mentionRoster.svelte';
	import { documentHalfSheetRoom } from '$lib/actions/documentHalfSheetRoom';
	import { documentFooterRoom } from '$lib/actions/documentFooterRoom';
	import { documentThreadChromeRoom } from '$lib/actions/documentThreadChromeRoom';
	import { createMentionSelection } from './comments/mentionSelection';
	import { keptThreads } from '$lib/docKeptThreads.svelte';
	import { keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';
	import { get } from 'svelte/store';
	import { Button } from './ui';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { peerFade } from '$lib/popoverMotion';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover, { anchorRect } from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import CommentList from './comments/CommentList.svelte';
	import SuggestionStatus from './comments/SuggestionStatus.svelte';
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import DocCommentImageHead from './DocCommentImageHead.svelte';
	import DocMarginStack from './DocMarginStack.svelte';
	import DocCommentDrawerHeading from './DocCommentDrawerHeading.svelte';
	import DocCommentDrawerFoot from './DocCommentDrawerFoot.svelte';
	import DocResolvedComments from './DocResolvedComments.svelte';
	import { createListThreadView } from './docCommentThreadView.svelte';
	import DocCommentThreadBack from './DocCommentThreadBack.svelte';
	import { holdReadingLine } from '$lib/docMarginHold.svelte';
	import { isShellControl } from '$lib/stores/paneCollapsed';
	import { createFormHandoff, settledCanvasWidth } from '$lib/docFormHandoff.svelte';
	import { createSuggestionDecisions } from '$lib/docSuggestionDecisions.svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import { retainUnreadList } from '$lib/docUnreadList.svelte';
	import { byCommentNumber, clickedOpener, drawerKeys, imageDrawerMotion, listAnchorRect, writingField, onSheetMoved, openerFocus, presence, scrollListToCard } from '$lib/docCommentDrawer';
	import { focusStillOurs } from '$lib/actions/focusTrap';
	import { restingTop, trackOverlaySheet } from '$lib/cm/visible-region';
	import { createCellLabels } from '$lib/docCommentLabels';
	import ThreadSheetActions from './comments/ThreadSheetActions.svelte';
	import { closeOnOwnResolve } from '$lib/docThreadClose.svelte';
	import { createCommentSheet } from '$lib/docCommentSheet.svelte';
	import { marginOut } from '$lib/docCommentDrawerMotion';
	import { marginOnCanvas } from '$lib/docMarginCanvas';
	import { imageGroupKey, onItsImage } from '$lib/docImageComments';
	import { refusalMessage, suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import { hasRoomForMargin, type CardMode, type CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
	import type { DocCommentSurfaceProps } from '$lib/docCommentSurfaceProps';
	let { commentTaskBar, state: surfaceState, anchor, anchorAtClick = false, bounds = null, protectReplyLanding = false, narrow, phone = null, onRelease, comments, widgetLabel, focusedId, focusedIds = null, ringId = null, draft, draftReason = '', onDraftBody, onCancelDraft, onClose, restoreFocus, onFocus, onSubmit, onReply, onResolve, onAgree, onViewed, target, docPath = null, artifactId, draftScope = '', feedback = undefined, readOnly = false, onSuggestionBusy, onSuggestionAccepted, onDocumentChanged, load, onRetry, image = null, images = null, covered = false, offScreen = false, onDrawerShown, onSheetModalChange, onRevealDraft, onRevealCard, onThreadMissing, quiet = false, draftOverColumn = false, opened = 0, resolvedOpen = $bindable(false), commentsEntry = null }: DocCommentSurfaceProps = $props();
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
	let footerScrolls = $state(false);
	const canvasWidth = settledCanvasWidth(() => bounds, () => form.note());
	// The margin column shows only when the text beside it keeps at least 45 characters per line. `hasRoomForMargin` is the canvas-width rule where that holds (the 308 px column and the canvas's padding leave 314 px of text, 45 characters of the editor font); below it the list is a popover from the comments control, and only a phone dims the document with a sheet. Image viewers already decide whether their canvas is a sheet.
	const roomForMargin = $derived(hasRoomForMargin(canvasWidth.value));
	const sheetDrawer = $derived(image ? narrow : (phone ?? narrow));
	/** How a card opened from the text shows: the phone sheet; else the margin column on any desktop canvas with room, cards beside their anchors; else a popover over the text. The editor reads it, so only a press opens a card over the text. */
	const columnFits = $derived(!narrow && !image && roomForMargin); // the one margin rule: the card mode and the column both read it
	export function cardMode(): CardMode { return (phone ?? narrow) ? 'sheet' : columnFits ? 'margin' : 'popover'; }
	/** What the column shows when it is up. */
	const forColumn = $derived(columnFits && (surfaceState?.kind === 'list' || surfaceState?.kind === 'thread' || (surfaceState?.kind === 'draft' && draftOverColumn)));
	const margin = $derived(forColumn);
	/** Whether the margin column is up: the editor's room beside the text follows this, never the leaving column's transition. */
	export function marginShown(): boolean { return margin; }
	const selectedId = $derived(surfaceState?.kind === 'thread' ? surfaceState.id : focusedId);
	// A thread whose text is gone keeps its card in the column at the nearest line, labelled "This text isn’t in the document" with a dashed edge (CommentThread). A review conversation never had text: its card, labelled "Review comment", sits after the last card that has a line.
	/** The comments control the list opened from; a list the canvas opened on load hangs from the header's control. */
	const listEntry = $derived(anchor instanceof HTMLElement ? anchor : commentsEntry ?? bounds?.querySelector<HTMLElement>('[data-comments-entry]') ?? null);
	/** The bar entry anchors the list to the row's bottom, so a clamped list cannot cover the bar. */
	const listAnchor = () => listAnchorRect(listEntry);
	const landingRect = () => protectReplyLanding ? bounds?.querySelector<HTMLElement>('.cm-comment-focused')?.getBoundingClientRect() ?? null : null;
	/** Extend the canvas area to the list anchor, including its horizontal position. */
	const listBounds = () => {
		const area = bounds?.getBoundingClientRect() ?? null;
		const entry = listEntry && !bounds?.contains(listEntry) ? listAnchor() : null;
		if (!area || !entry) return area;
		const left = Math.min(area.left, entry.left), top = Math.min(area.top, entry.bottom);
		return new DOMRect(left, top, Math.max(area.right, entry.right) - left, area.bottom - top);
	};
	// A deliberate pick returns the column to that card; passive updates leave the chosen view alone.
	let lastOpened: number | undefined;
	$effect.pre(() => { const now = opened; if (lastOpened !== undefined && now !== lastOpened && !sheetDrawer && !(surfaceState?.kind === 'list' && resolvedOpen && comments.some((comment) => comment.id === focusedId && comment.resolved))) resolvedOpen = false; lastOpened = now; });
	const form = createFormHandoff({ surfaceKey: () => surfaceState === null ? '' : `${surfaceState.kind}:${'id' in surfaceState ? surfaceState.id : ''}`, formKey: () => surfaceState === null ? '' : margin ? 'margin' : cardMode(),
		drawer: () => drawer, writing: (active) => surfaceState ? writingField(active, comments, surfaceState.kind === 'draft') : null });
	holdReadingLine({ margin: () => margin, bounds: () => bounds, subject: () => (surfaceState?.kind === 'draft' ? 'draft' : selectedId) });
	let draftSelection = $state<[number, number] | null>(null); $effect(() => { if (!draft) draftSelection = null; }); // the composer's caret, kept across a redraw of its form
	let detent = $state<'full' | 'half'>('full');
	/** The list's thread view: one thread whole in the same sheet or drawer, with Back to the list where it was. */
	const threadView = createListThreadView(() => drawer);
	const openedComment = $derived(!margin && surfaceState?.kind === 'list' && threadView.id ? comments.find((comment) => comment.id === threadView.id) ?? null : null);
	const draftTags = createMentionSelection();
	$effect(() => { if (!draft) draftTags.clear(); });
	const replyText = (id: string) => keptCommentDrafts.get(replyDraftKey(draftScope, id))?.body ?? '';
	// A thread someone else resolves while a reply to it is being typed stays in the Open list, as a resolved card, until that reply is sent or cleared.
	/** The document's switch selects the existing resolved threads; images keep their own filter. */
	const view = $derived(image ? filter : resolvedOpen ? 'resolved' : 'open');
	const held = keptThreads({ comments: () => comments, draft: replyText, leavesOnRelease: () => view === 'open', reduced: () => get(prefersReducedMotion), crossfaded: () => get(prefersReducedMotion) && sheetDrawer && surfaceState?.kind === 'list' });
	const unreadListed = retainUnreadList(() => surfaceState?.kind === 'list', () => comments);
	const column = $derived(bounds?.closest<HTMLElement>('.leaf-body') ?? bounds);
	const openCount = $derived(comments.filter((comment) => !comment.resolved).length);
	// Open counts threads that are open: the resolved card kept for an unsent reply says why it is still listed, and the document's chip agrees.
	// A failed load with nothing known claims no number: a zero there would say the document has no discussions.
	const headerCount = $derived((load?.state === 'error' || load?.state === 'loading') && comments.length === 0 ? '' : view === 'open' ? `${openCount}` : view === 'resolved' ? `${comments.length - openCount} resolved` : `${comments.length}`);
	const sheet = createCommentSheet({
		state: () => surfaceState, draft: () => draft, sheetDrawer: () => sheetDrawer,
		narrow: () => cardMode() === 'sheet',
		onClose: () => onClose(),
		onCancelDraft: () => (onCancelDraft ?? onClose)(),
	});
	// The sheet owns this lifecycle; the viewer only consumes its signal to avoid showing another control through its scrim.
	$effect(() => { onSheetModalChange?.(sheet.modalOpen); });
	$effect(() => () => onSheetModalChange?.(false));
	// The editor keeps its text above whichever of these sheets is up: it reads where it rests from `trackOverlaySheet`.
	let listSheet = $state<HTMLElement | null>(null);
	let draftSheet = $state<HTMLElement | null>(null);
	/** The resting edge, even while the sheet is entering, for the image viewer's visible area. */
	export function sheetTop(): number | null {
		const panel = sheet.state?.kind === 'list' && sheetDrawer ? listSheet : draftSheet;
		return sheet.open && panel ? restingTop(panel) : null;
	}
	$effect(() => trackOverlaySheet(bounds, () => covered || !sheet.open ? null : sheet.state?.kind === 'list' && sheetDrawer ? listSheet : draftSheet));
	// The draft sheet exists only at phone width; a narrow canvas's composer is a popover, with no sheet to keep the subject above.
	$effect(() => onSheetMoved(surfaceState?.kind === 'draft' ? draftSheet : surfaceState?.kind === 'thread' && cardMode() === 'sheet' ? draftSheet : null, surfaceState?.kind === 'draft' ? onRevealDraft : onRevealCard && ((opening) => onRevealCard(opening, draftSheet))));
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
	const resolvedList = $derived(comments.filter((comment) => comment.resolved && !held.kept(comment) && !held.leaving.has(comment.id) && !comment.review).sort(byNewest));
	/** The count the drawer's Resolved answer shows. Nothing outside the surface reads it, so it stays here. */
	const resolvedCount = $derived(image ? 0 : resolvedList.length);
	/** The phone sheet's switch is the heading's meta, beside the title, so it takes no row of its own; the popover and the margin column keep the row. */
	const switchInHeading = $derived(!margin && !isImage && !openedComment && sheet.state?.kind === 'list' && !!sheetDrawer);
	const displayed = $derived.by(() => {
		const showing = sheet.state;
		if (showing?.kind === 'thread') return comments.filter((comment) => showing.ids ? showing.ids.includes(comment.id) : comment.id === showing.id);
		// An image's comments read in number order, like its tags.
		return image ? [...allShown].sort(byCommentNumber) : allShown;
	});
	/** A phone sheet showing one thread carries that thread's Agree and Resolve in its header row, apart from Close, so the card draws neither. */
	const threadActionsUp = $derived(cardMode() === 'sheet' && sheet.state?.kind === 'thread' && displayed.length === 1);
	const threadHead = $derived(threadActionsUp && !readOnly ? displayed[0] : undefined);
	const listThreadHead = $derived(!isImage && cardMode() === 'sheet' && sheet.state?.kind === 'list' ? openedComment : null);
	const ownClose = closeOnOwnResolve(() => listThreadHead ?? (threadActionsUp ? displayed[0] : undefined), (id) => !!replyText(id).trim(), () => { if (listThreadHead) threadView.back(); else onClose(); });
	const stackedTitle = $derived(sheet.state?.kind === 'thread' && sheet.state.ids && sheet.state.ids.length > 1 ? `${sheet.state.ids.length} comments` : null);
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
		filter = 'all'; filterOpen = false; navigationNotice = ''; detent = 'full'; threadView.reset();
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
	/** Escape in the thread view goes back to the list; in the list it closes. */
	function escapeList() { if (openedComment) threadView.back(); else closeDrawer(); }
	// In the margin Escape lets a picked card go and focus goes back to where it came from (the caret, from the text); the column stays.
	function escapeColumn() { if (!margin) { closeDrawer(); return; } onRelease?.(); restoreFocus(); }
	// Resolving a decided suggestion ends its conversation, and its edit is already settled in the text, so its card closes with it (DRAFT-comment-fixes-3to6, Option A step 5; in the margin the card leaves the column by itself and the column stays up), but only once the request is answered: on a refusal the card stays as it was, with focus where it was, and says so through this opening's card errors, which reset with the decisions' when the surface reopens.
	function resolveThread(id: string, resolved: boolean): Promise<unknown> {
		if (!(resolved && (margin || (surfaceState?.kind === 'thread' && surfaceState.id === id)) && comments.find((item) => item.id === id)?.decision)) return Promise.resolve(onResolve(id, resolved));
		return decisions.resolveDecided(id, () => onResolve(id, true, true)).then((taken) => {
			if (!taken || margin || surfaceState?.kind !== 'thread' || surfaceState.id !== id) return;
			onClose(); if (cardMode() !== 'sheet') restoreFocus();
		});
	}
	function resolveFromList(id: string, resolved: boolean) {
		const answered = resolveThread(id, resolved);
		if (resolved && listThreadHead?.id === id) ownClose.resolving(id, answered);
		return answered;
	}
	function finishSheetClose() {
		const returnFocus = focusStillOurs(listSheet ?? draftSheet, document.activeElement);
		sheet.finishClose();
		if (!sheet.open && returnFocus) void tick().then(() => restoreFocus());
	}
	// The list popover moves to the page body after its threads mount, and the move resets its scroll: the picked thread is brought into view once, when it is first placed.
	const revealed = new WeakSet<HTMLElement>();
	function revealPicked(card: HTMLElement) { if (!revealed.has(card)) { revealed.add(card); card.querySelector('[data-comment-id].focused')?.scrollIntoView({ block: 'nearest' }); } }
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
		onSuggestionBusy: (...args) => onSuggestionBusy?.(...args), onSuggestionAccepted: (text, history) => history ? onSuggestionAccepted?.(text, history) : onSuggestionAccepted?.(text), onDocumentChanged: () => onDocumentChanged?.(),
	});
	/** On the phone sheet the Accept suggestions row sits inside the scroller (pinned to its top at rest); every other list form keeps it above the scroller. It is in one place for the life of the sheet, so the keyboard never rebuilds it. */
	const acceptInScroller = $derived(!margin && sheet.state?.kind === 'list' && sheetDrawer);
	/** Accept suggestions shows in every list form outside the margin, when there is something to say. */
	const acceptAllShown = $derived((!margin || resolvedOpen || !isImage) && !readOnly && !openedComment && !!(decisions.authorsWithOpenSuggestions.length || decisions.outcome.skipped.length || decisions.outcome.acceptAllError));
	// Stale and missing-file accept refusals go to `suggestionRefusals`, which also shows the card as orphaned.
	const cardErrors = $derived({
		...Object.fromEntries(Object.entries(artifactId ? $suggestionRefusals[artifactId] ?? {} : {}).map(([id, refusal]) => [id, refusalMessage(refusal)])),
		...Object.fromEntries(Object.entries(decisions.outcome.errors).filter(([, message]) => message)),
	});
</script>

{#snippet imageHead(row: unknown[])}<DocCommentImageHead {row} {images} />{/snippet}
{#snippet threadList(items: DocComment[], reply?: boolean, inMargin?: boolean, underTextChanged?: boolean, flat?: boolean, replyView: 'recent' | 'all' | 'summary' = 'recent', actionsAbove?: boolean)}
	<CommentList {actionsAbove} {replyView} onOpenThread={replyView === 'summary' ? threadView.open : undefined} {draftScope} {underTextChanged} {flat} docPath={target?.path ?? docPath} leavingIds={held.leaving} keptIds={new Set(comments.filter(held.kept).map((comment) => comment.id))} reduced={$prefersReducedMotion} comments={items} {cellLabel} documentOrder={inMargin || sheet.state?.kind === 'list'} {focusedId} {focusedIds} {ringId} restCards={!!inMargin && !flat} candidateOrder={image?.candidateOrder ?? null} hoveredId={image?.hoveredId ?? null} onHover={image?.onHover} scrollOnFocus={!isImage && !inMargin} pressKeepsPlace={!isImage} focusReply={isImage && sheet.state?.kind === 'thread'} showReply={reply ?? shouldShowReply} keepReplyVisible={sheetDrawer && sheet.state?.kind === 'list'} onViewed={covered || offScreen ? undefined : onViewed} onFocus={sheet.state?.kind === 'list' && !inMargin ? navigate : onFocus} {onReply} onResolve={resolveFromList} {onAgree} onDecision={decisions.decideOne} decisionErrors={cardErrors} tagColor={image?.colorOf ?? (images ? (comment) => images.colorOf(asDoc(comment)) : undefined)} onMovePin={image?.onMovePin ?? (images ? movePinInDocument : undefined)} groupOf={images ? (comment) => imageGroupKey(asDoc(comment), !!images.diagramOf?.(asDoc(comment))) : undefined} diagramOf={(comment) => image?.diagram ?? images?.isDiagram?.(asDoc(comment)) ?? false} groupHead={images ? imageHead : undefined} {readOnly} reopenReachesOwner={load?.state === 'ready' && load.owner != null} />
{/snippet}
{#snippet threadActions()}{#if threadHead}<ThreadSheetActions head={threadHead} onAgree={onAgree} onResolve={(id) => ownClose.resolving(id, resolveThread(id, true))} />{/if}{/snippet}
<!-- In the margin the column's head card (DocMarginStack), which takes no room unless there is something to say. -->
{#snippet statusSwitch()}<div class="drawer-switch" class:in-heading={switchInHeading} role="group" aria-label="Comment status"><button type="button" aria-pressed={!resolvedOpen} onclick={() => resolvedOpen = false}>Open · {openCount}</button><button type="button" data-resolved-entry aria-pressed={resolvedOpen} onclick={() => resolvedOpen = true}>Resolved · {resolvedCount}</button></div>{/snippet}
{#snippet acceptAllBar()}
	<SuggestionStatus authors={decisions.authorsWithOpenSuggestions} busy={decisions.acceptAllBusy} shownBusy={decisions.outcome.acceptAllShownBusy} skipped={decisions.outcome.skipped} error={decisions.outcome.acceptAllError} acceptAll={decisions.acceptAllFrom} />
{/snippet}
{#snippet content()}
	{#if sheet.state?.kind === 'draft' && sheet.draft}<CommentComposer shownKey={draftScope} selectedTags={sheet.draft?.mentionSelection ?? draftTags} quote={sheet.draft.text} label={sheet.draft.label} body={sheet.draft.body ?? ''} disabledReason={draftReason} error={sheet.draft.error ?? ''} onBody={onDraftBody} goesTo={load?.state === 'ready' ? (load.goesTo?.length ? { state: 'agent', agents: load.goesTo } : { state: 'none' }) : load ? { state: load.state } : undefined} {onRetry} selection={draftSelection} onSelection={(selection) => draftSelection = selection} onSubmit={onSubmit} onCancel={sheet.cancelDraft} onEscape={sheet.close} />
	{:else}
		{#if stackedTitle && cardMode() !== 'sheet'}<h2 class="stacked-title">{stackedTitle}</h2>{/if}
		<!-- One thread in the popover or the sheet draws flat: the container is its card. A row in the list shows its newest reply and opens whole in the thread view. -->
		{@render threadList(displayed, undefined, undefined, sheet.state?.kind === 'thread', sheet.state?.kind === 'thread' && displayed.length === 1, sheet.state?.kind === 'list' ? 'summary' : 'recent', threadActionsUp)}
	{/if}
{/snippet}
{#snippet listBody()}
	<!-- On the canvas the open threads are cards beside their text, with no heading or close around them: the column closes with the comments control. -->
	{#if !margin}<DocCommentDrawerHeading count={headerCount} meta={switchInHeading ? statusSwitch : undefined} filtered={!!image} {commentTaskBar} bind:filter bind:filterOpen onClose={closeDrawer} />{/if}
	{#if !isImage && !openedComment && !switchInHeading}{@render statusSwitch()}{/if}
	{#if load?.state === 'loading'}<p class="drawer-status">Loading comments…</p>{/if}
	{#if load?.state === 'error'}<div class="drawer-status drawer-error" role="status"><span>Couldn't load comments. Check the connection and retry.</span><Button size="sm" onclick={onRetry}>Retry</Button></div>{/if}
	{#if navigationNotice}<p class="drawer-status">{navigationNotice}</p>{/if}
	<!-- Accept suggestions sits above the one scroller in every list form outside the margin (there it is the column's head card), so it is never scrolled away; it takes no room unless there is something to say. On the phone sheet it is the scroller's first row, pinned to its top, so it looks and stays where it was; under the keyboard the footer scrolls to give the list room (documentFooterRoom) and the row scrolls with it. The thread view gives that room to its one thread. -->
	{#if acceptAllShown && !acceptInScroller}<div class="list-accept-all">{@render acceptAllBar()}</div>{/if}
	{#if openedComment}<DocCommentThreadBack onBack={threadView.back} />{/if}
	{#if margin && !isImage && artifactId}<DocCommentDrawerFoot {feedback} />{/if}
	<div class="drawer-comments" class:in-margin={margin}>
		{#if acceptAllShown && acceptInScroller}<div class="list-accept-all in-scroller" class:pinned={!footerScrolls}>{@render acceptAllBar()}</div>{/if}
		{#if image && image.showThumbnail !== false && displayed.some((comment) => comment.region)}<ImageRegionThumb src={image.src} alt={image.alt} comments={displayed} colorOf={image.colorOf} onOpen={image.onOpen} />{/if}
		<div class="peers">{#key view}<div class="peer" in:peerFade out:peerFade>
		{#if !isImage && resolvedOpen}<DocResolvedComments>{#snippet list()}{#if resolvedList.length}{@render threadList(resolvedList, true, true, false, true)}{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">No resolved comments.</p>{/if}{/snippet}</DocResolvedComments>{:else if (allShown.length || marginDraft) && margin}<DocMarginStack {bounds} {pinned} {selectedId} composer={marginDraft} focusComposer={!form.redrawn} head={isImage && !readOnly && (decisions.authorsWithOpenSuggestions.length || decisions.outcome.skipped.length || decisions.outcome.acceptAllError) ? acceptAllBar : null}>{#snippet card(comment)}{@render threadList([comment], true, true, true)}{/snippet}</DocMarginStack>{:else if openedComment}{@render threadList([openedComment], true, false, false, true, 'all')}{:else if displayed.length}{@render content()}{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">{comments.length === 0 ? 'No comments yet.' : view === 'open' ? 'No open comments.' : 'No resolved comments.'}{#if image && filter === 'resolved' && openCount}<button type="button" class="drawer-empty-action" onclick={() => filter = 'open'}>Show open · {openCount}</button>{/if}</p>{/if}
		</div>{/key}</div>
		{#if footerScrolls && !isImage && artifactId}<DocCommentDrawerFoot {feedback} />{/if}
	</div>
	{#if !margin && !isImage && artifactId && !footerScrolls}<DocCommentDrawerFoot {feedback} />{/if}
{/snippet}
{#snippet cardPopover()}<Popover fill anchor={cardAnchor} placement={anchorAtClick ? 'pointer' : 'right'} {bounds} clearAnchor={surfaceState?.kind === 'thread' && !image} avoidRect={image?.imageBounds} initialFocus={form.handoff ?? (image && !readOnly ? '.ct-input' : undefined)} takeFocus={form.redrawn ? !!form.handoff : !(surfaceState?.kind === 'thread' && surfaceState.fromText)} sideKey={surfaceState?.kind === 'thread' ? surfaceState.ids?.[0] ?? surfaceState.id : anchor} open={true} {onClose} {restoreFocus} keepOpenOnPointer={(target) => isShellControl(target) || (!!image && surfaceState?.kind === 'draft' && target instanceof Element && !!bounds?.contains(target) && !!target.closest('[data-image-zoom]'))}>{@render content()}</Popover>{/snippet}

{#if surfaceState || sheet.closing}
	{#if margin && surfaceState}
		<section class="canvas-comment-drawer margin" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:marginOnCanvas={{ root: bounds, scroller: '.cm-scroller' }} use:listDrawerShown use:drawerKeys={escapeColumn} onclickcapture={(event) => opener = clickedOpener(event)} out:marginOut|global>{@render listBody()}</section>
	{:else if sheet.state?.kind === 'list' && sheetDrawer}
		<AdaptiveDrawer bind:panel={listSheet} ariaLabel="Document comments" initialFocusSelector={focusedIds ? '[data-comment-id].focused .ct-num' : '[data-drawer-autofocus]'} headless fill opaque {detent} onDetent={(next) => detent = next} followKeyboard={narrow} within={column} {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose}><section class="canvas-comment-drawer sheet" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:documentHalfSheetRoom use:documentFooterRoom={(scrolls) => footerScrolls = scrolls} use:documentThreadChromeRoom use:listDrawerShown use:drawerKeys={escapeList} onclickcapture={(event) => opener = clickedOpener(event)}>{@render listBody()}</section></AdaptiveDrawer>
	{:else if surfaceState?.kind === 'list' && !image && !columnFits}
		<!-- Below the column's threshold on a desktop canvas: a popover from the comments control, so nothing is dimmed or covered but the list itself. -->
		<Popover anchor={listAnchor} placement="bottom-end" bounds={listBounds} avoidRect={landingRect} fill {covered} initialFocus={form.handoff ?? undefined} takeFocus={!form.redrawn || !!form.handoff} open={true} {onClose} {restoreFocus} onPlace={revealPicked} keepOpenOnPointer={(target) => isShellControl(target) || (target instanceof Node && !!listEntry?.contains(target))}><section class="canvas-comment-drawer list-pop" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={escapeList} onclickcapture={(event) => opener = clickedOpener(event)}>{@render listBody()}</section></Popover>
	{:else if surfaceState?.kind === 'list' && image}
		<section class="canvas-comment-drawer" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={escapeList} onclickcapture={(event) => opener = clickedOpener(event)} in:drawerMotion|global={{ leaving: false }} out:drawerMotion|global={{ leaving: true }}>{@render listBody()}</section>
	{:else if cardMode() === 'sheet' && sheet.state}<AdaptiveDrawer keepsText fill={sheet.state.kind === 'draft'} bind:panel={draftSheet} ariaLabel={stackedTitle ?? "Document comments"} heading={sheet.state.kind === 'draft' ? 'New comment' : stackedTitle} doneLabel="Close" opaque followKeyboard {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose} actions={threadHead ? threadActions : undefined}>{#if sheet.state.kind === 'thread'}<div class="sheet-thread" data-sheet-thread>{@render content()}</div>{:else}{@render content()}{/if}</AdaptiveDrawer>
	{:else if surfaceState && surfaceState.kind !== 'list' && !forColumn}{@render cardPopover()}{/if}
{/if}
<style>
	.drawer-switch { display: flex; gap: var(--space-1); }
	.drawer-switch button { min-height: var(--touch-target); padding: 0 var(--space-2); border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-secondary); font-family: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer; }
	.drawer-switch button[aria-pressed="true"] { color: var(--accent-text); font-weight: var(--fw-semibold); }
	/* In the sheet's heading the switch reads as the title's meta, in the Plan view's meta type, and takes no more room than its two targets, which never shrink below their text (the heading wraps the switch under the title instead). */
	.drawer-switch.in-heading { gap: 0; flex: none; }
	.drawer-switch.in-heading button { min-width: var(--touch-target); padding: 0 var(--space-1); flex: none; font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; white-space: nowrap; }
	.canvas-comment-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; padding: var(--space-1) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
	/* The column is drawn in the editor's scroller (`marginOnCanvas`), in the room the editor keeps right of its text (DocViewer.css), with no box, rule or heading of its own; until it is there it is not drawn. */
	.canvas-comment-drawer.margin { display: none; position: absolute; z-index: 1; top: 0; right: var(--space-2); width: calc(var(--margin-column) - var(--space-3) - var(--space-2)); padding: 0; border: 0; }
	.canvas-comment-drawer.margin:global(.on-canvas) { display: flex; }
	/* The column grows in from nothing; its content keeps the final width so the reply fields measure at it. */
	.canvas-comment-drawer.margin > :global(*) { min-width: 287px; }
	/* The column never scrolls on its own: its cards are part of the page and overflow it downward, so the scroller reaches them. */
	.drawer-comments.in-margin { position: relative; overflow: visible; scrollbar-gutter: auto; }
	.stacked-title { margin: 0; padding: var(--space-1) var(--space-3) var(--space-1); color: var(--text-secondary); font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
	.canvas-comment-drawer.sheet { flex: 1; padding: 0; border-top: 0; }
	/* A short sheet (a phone held sideways under its keyboard) puts Back on the heading's row, fixed and 44px tall, so a reply field, its "Goes to" line and Send fit the one scroller (documentThreadChromeRoom). */
	.canvas-comment-drawer.sheet:global([data-short-chrome]) { position: relative; --back-room: 0px; }
	.canvas-comment-drawer.sheet:global([data-short-chrome]) > :global(.drawer-heading) { min-height: var(--touch-target); padding-left: calc(var(--space-1) + var(--back-room)); }
	.canvas-comment-drawer.sheet:global([data-short-chrome]) > :global(.thread-back) { position: absolute; top: 0; left: 0; display: flex; height: var(--touch-target); }
	.canvas-comment-drawer.sheet:global([data-short-chrome]) :global(.drawer-back) { min-height: var(--touch-target); }
	/* The single-thread sheet shows every reply in full, so its card's reply count gives its room to the author's name. */
	.sheet-thread { display: contents; }
	.sheet-thread :global(.ct-reply-count) { display: none; }
	.canvas-comment-drawer.list-pop { flex: 1; padding: 0; border: 0; }
	@container doc-canvas (min-width: 900px) { .canvas-comment-drawer { border-top: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-3); } }
	/* A filter switch crossfades: the leaving and arriving lists share one grid cell, so neither moves the other. */
	.peers { display: grid; }
	.peer { grid-area: 1 / 1; min-width: 0; }
	.drawer-comments { position: relative; flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
	/* In a cramped sheet the scrolling footer's row must fit beside a 44px reply target. */
	.drawer-comments :global(.drawer-foot .tg.row) { min-height: var(--touch-target); }
	.drawer-status { color: var(--text-secondary); font-size: var(--text-footnote); margin: 4px 0; }
	/* Retry sits on its own row under the sentence, so it never touches the end of a wrapped line. */
	.drawer-error { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); }
	.drawer-empty-action { margin-left: var(--space-1); padding: 0; border: 0; background: none; color: var(--accent-text); font: inherit; font-weight: var(--fw-semibold); cursor: pointer; }
	.drawer-empty-action:hover { text-decoration: underline; }
	.list-accept-all { flex: none; padding: 0 0 var(--space-1) var(--space-1); }
	/* In the scroller the row stays at its top at rest, where it sat above it; the drawer's row gap above the scroller is now this padding. */
	.list-accept-all.in-scroller { padding-bottom: calc(var(--space-1) * 2); }
	/* Pinned, the row has its own ground and nothing past its box, so the list never gains sideways room. In a classic-scrollbar browser a selected card scrolled under it can show its 3px ring in the gutter beside the row. */
	.list-accept-all.in-scroller.pinned { position: sticky; top: 0; z-index: 1; background: var(--bg-secondary); }
</style>
