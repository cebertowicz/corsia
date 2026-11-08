<script lang="ts">
	import { tick, type Snippet } from 'svelte';
	import { loadMentionRoster } from './comments/mentionRoster.svelte';
	import { createMentionSelection, type MentionOccurrence } from './comments/mentionSelection';
	import { keptThreads } from '$lib/docKeptThreads.svelte';
	import { get } from 'svelte/store';
	import { Button, IconButton } from './ui';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { menuPop, peerFade } from '$lib/popoverMotion';
	import type { CommentLoad } from '$lib/stores/docComments';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import CommentList from './comments/CommentList.svelte';
	import SuggestionStatus from './comments/SuggestionStatus.svelte';
	import CommentPlacementRecovery, { type PlacementRecoveryProps } from './comments/CommentPlacementRecovery.svelte';
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import DocCommentImageHead from './DocCommentImageHead.svelte';
	import DocMarginStack from './DocMarginStack.svelte';
	import { createMarginAside } from '$lib/docMarginAside.svelte';
	import { holdReadingLine } from '$lib/docMarginHold.svelte';
	import { createSuggestionDecisions } from '$lib/docSuggestionDecisions.svelte';
	import type { DocComment } from '$lib/stores/docComments';
	import { retainUnreadList } from '$lib/docUnreadList.svelte';
	import { COMMENT_FILTERS as FILTERS, byCommentNumber, clickedOpener, drawerFooterHeight, drawerKeys, imageDrawerMotion, observeCanvasWidth, observeSheetTyping, onSheetMoved, openerFocus, presence, scrollListToCard } from '$lib/docCommentDrawer';
	import { focusStillOurs } from '$lib/actions/focusTrap';
	import { restingTop, trackOverlaySheet } from '$lib/cm/visible-region';
	import { createCellLabels, type WidgetLabelReader } from '$lib/docCommentLabels';
	import { createCommentSheet } from '$lib/docCommentSheet.svelte';
	import { asidePop, marginOut } from '$lib/docCommentDrawerMotion';
	import { imageGroupKey, onItsImage, type ImageCommentSurface, type DocImageComments } from '$lib/docImageComments';
	import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import { hasRoomForMargin, type CommentAnchor, type CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
	let { state: surfaceState, anchor, anchorAtClick = false, bounds = null, narrow, phone = null, onRelease, comments, widgetLabel, focusedId, focusedIds = null, draft, draftReason = '', onDraftBody, onCancelDraft, onClose, restoreFocus, onFocus, onSubmit, onReply, onResolve, onAgree, onViewed, target, docPath = null, artifactId, readOnly = false, onSuggestionBusy, onSuggestionAccepted, onDocumentChanged, load, onRetry, image = null, images = null, placementRecovery = { unplacedCount: 0, replaceableCount: 0 }, covered = false, readStampSuppressed = false, onDrawerShown, onSheetModalChange, onRevealDraft, onThreadMissing, reviewBar, reviewVisible = false, quiet = false }: {
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
		/** Re-resolves orphaned live comment ranges from the existing comments drawer. */
		placementRecovery?: PlacementRecoveryProps;
		/** An image opened from the document covers it, the drawer with it: the drawer stays open beneath, and gets its focus back after. */
		covered?: boolean;
		readStampSuppressed?: boolean;
		/** Whether the comment list's drawer is on screen, in either form: true from its mount until its outro has ended. */
		onDrawerShown?: (shown: boolean) => void;
		/** The sheet controller's modal presence, including its closing scrim. */
		onSheetModalChange?: (open: boolean) => void;
		/** Asks the editor to keep the draft's text clear of the sheet, now and whenever the sheet moves. */
		onRevealDraft?: (opening: boolean) => void;
	} = $props();
	loadMentionRoster(() => ({ artifactId, threadId: target?.threadId ?? null }));
	const asDoc = (comment: unknown) => comment as DocComment;
	const cellLabel = createCellLabels(() => widgetLabel);
	const listDrawerShown = presence((shown) => onDrawerShown?.(shown));
	function movePinInDocument(id: string) {
		const comment = comments.find((item) => item.id === id);
		if (comment && onItsImage(comment)) images?.onOpen(comment, { id, move: true });
	}
	/** The image drawer's own Filter. A document's column shows its open threads as cards and lists only resolved ones (`view`). */
	let filter = $state<'all' | 'open' | 'resolved'>('all');
	let filterOpen = $state(false);
	/** The sheet's resolved threads, open under their entry. */
	let sheetResolvedOpen = $state(false);
	const filterName = $derived(FILTERS.find(([key]) => key === filter)?.[1] ?? 'Open');
	let drawer = $state<HTMLElement>();
	let canvasWidth = $state(Number.POSITIVE_INFINITY);
	$effect(() => observeCanvasWidth(bounds, (width) => canvasWidth = width));
	// The margin column shows only when the text beside it keeps at least 45 characters per line. `hasRoomForMargin` is the canvas-width rule where that holds (the 308 px column and the canvas's padding leave 314 px of text, 45 characters of the editor font); below it, today's sheet. Image viewers already decide whether their canvas is a sheet.
	const roomForMargin = $derived(hasRoomForMargin(canvasWidth));
	const sheetDrawer = $derived(narrow || (!image && !roomForMargin));
	const cardAsSheet = $derived(phone ?? narrow);
	// Margin column on any desktop canvas with room, cards beside their anchors.
	const wantsRoom = $derived(!narrow && !image && (surfaceState?.kind === 'list' || surfaceState?.kind === 'thread'));
	const margin = $derived(wantsRoom && roomForMargin);
	const selectedId = $derived(surfaceState?.kind === 'thread' ? surfaceState.id : focusedId);
	// A thread whose text is gone keeps its card in the column at the nearest line, labelled "This text is no longer in the document" (CommentThread). A review conversation never had text: it keeps its own entry and list, labelled "Review comment".
	const aside = createMarginAside({ margin: () => margin, selectedId: () => selectedId, resolved: () => resolvedList, reviews: () => reviews });
	holdReadingLine({ margin: () => margin, bounds: () => bounds, selectedId: () => selectedId });
	let detent = $state<'full' | 'half'>('full');
	const draftTags = createMentionSelection();
	const replyTags = new Map<string, ReturnType<typeof createMentionSelection>>();
	$effect(() => { if (!draft) draftTags.clear(); });
	let replyDrafts = $state<Record<string, string>>({});
	let replySelections = $state<Record<string, [number, number]>>({});
	// A thread someone else resolves while a reply to it is being typed stays in the Open list, as a resolved card, until that reply is sent or cleared.
	/** What the list holds. A document's column keeps its open threads as cards in the margin and lists the resolved ones in its own box; the sheet lists the open threads and, under them, the resolved ones behind an entry; an image's drawer has its own Filter. */
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
		narrow: () => cardAsSheet,
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
	/** Every open thread is a card in the margin, a deleted anchor's included. */
	const pinned = $derived(allShown.filter((comment) => !comment.review));
	const resolvedList = $derived(comments.filter((comment) => isResolved(comment) && !comment.review).sort(byNewest));
	const reviews = $derived(allShown.filter((comment) => !!comment.review));
	/** The column's open list covers the cards, so they are not drawn while it shows. */
	const coversCards = $derived(aside.open && (aside.kind === 'resolved' ? resolvedList : reviews).length > 0);
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
		filter = 'all'; filterOpen = false; sheetResolvedOpen = false; navigationNotice = ''; detent = 'full';
		void tick().then(() => {
			const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
			// An image's drawer opens at the selected comment instead (the effect below).
			if (list && !(isImage && focusedId)) list.scrollTop = 0;
			// A column opened on load (quiet) leaves focus where it was.
			if (!quiet) drawer?.querySelector<HTMLElement>(focusedIds ? '[data-comment-id].focused .ct-num' : '[data-drawer-filter], [data-aside-kind="resolved"]')?.focus({ preventScroll: true });
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
	function closeDrawer() { onClose(); restoreFocus(); }
	// In the margin the first Escape lets the picked card go (focus back to where it came from); the next closes the column.
	function escapeColumn() { if (margin && onRelease?.()) restoreFocus(); else closeDrawer(); }
	// Resolving a decided suggestion ends its conversation, and its edit is already settled in the text, so its card closes with it (DRAFT-comment-fixes-3to6, Option A step 5; in the margin the card leaves the column by itself and the column stays up), but only once the request is answered: on a refusal the card stays as it was, with focus where it was, and says so through this opening's card errors, which reset with the decisions' when the surface reopens.
	function resolveThread(id: string, resolved: boolean) {
		if (!(resolved && (margin || (surfaceState?.kind === 'thread' && surfaceState.id === id)) && comments.find((item) => item.id === id)?.decision)) { void onResolve(id, resolved); return; }
		void decisions.resolveDecided(id, () => onResolve(id, true, true)).then((taken) => {
			if (!taken || margin || surfaceState?.kind !== 'thread' || surfaceState.id !== id) return;
			onClose(); if (!cardAsSheet) restoreFocus();
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
	<CommentList {replyTags} {underTextChanged} {flat} docPath={target?.path ?? docPath} bind:replyDrafts bind:replySelections leavingIds={held.leaving} keptIds={new Set(comments.filter(held.kept).map((comment) => comment.id))} reduced={$prefersReducedMotion} comments={items} {cellLabel} documentOrder={inMargin || sheet.state?.kind === 'list'} {focusedId} {focusedIds} restCards={!!inMargin && !flat} candidateOrder={image?.candidateOrder ?? null} hoveredId={image?.hoveredId ?? null} onHover={image?.onHover} scrollOnFocus={!isImage && !inMargin} focusReply={isImage && sheet.state?.kind === 'thread'} showReply={reply ?? shouldShowReply} keepReplyVisible={sheetDrawer && sheet.state?.kind === 'list'} onViewed={covered || readStampSuppressed || !surfaceState ? undefined : onViewed} onFocus={sheet.state?.kind === 'list' && !inMargin ? navigate : onFocus} {onReply} onResolve={resolveThread} {onAgree} onDecision={decisions.decideOne} decisionErrors={cardErrors} tagColor={image?.colorOf ?? (images ? (comment) => images.colorOf(asDoc(comment)) : undefined)} onMovePin={image?.onMovePin ?? (images ? movePinInDocument : undefined)} groupOf={images ? (comment) => imageGroupKey(asDoc(comment), !!images.diagramOf?.(asDoc(comment))) : undefined} diagramOf={(comment) => image?.diagram ?? images?.isDiagram?.(asDoc(comment)) ?? false} groupHead={images ? imageHead : undefined} {readOnly} reopenReachesOwner={load?.state === 'ready' && load.owner != null} />
{/snippet}
{#snippet sheetResolved()}
	<div class="sheet-resolved">
		<div class="margin-aside-entry"><button type="button" class="filter-button aside-button" data-aside-kind="resolved" aria-expanded={sheetResolvedOpen} onclick={() => sheetResolvedOpen = !sheetResolvedOpen}>Resolved · {resolvedList.length} <svg class="aside-chevron" class:open={sheetResolvedOpen} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button></div>
		{#if sheetResolvedOpen}{@render threadList(resolvedList)}{/if}
	</div>
{/snippet}
{#snippet acceptAllBar()}
	<SuggestionStatus authors={decisions.authorsWithOpenSuggestions} busy={decisions.acceptAllBusy} shownBusy={decisions.outcome.acceptAllShownBusy} skipped={decisions.outcome.skipped} error={decisions.outcome.acceptAllError} acceptAll={decisions.acceptAllFrom} />
{/snippet}
{#snippet content()}
	{#if sheet.state?.kind === 'draft' && sheet.draft}<CommentComposer selectedTags={sheet.draft?.mentionSelection ?? draftTags} quote={sheet.draft.text} label={sheet.draft.label} body={sheet.draft.body ?? ''} disabledReason={draftReason} onBody={onDraftBody} onSubmit={onSubmit} onCancel={sheet.cancelDraft} />
	{:else}
		{#if !readOnly && sheet.state?.kind !== 'thread'}{@render acceptAllBar()}{/if}
		{@render threadList(displayed, undefined, undefined, sheet.state?.kind === 'thread')}
	{/if}
{/snippet}
{#snippet rowHost(above = true)}<div class="rb-sheet-host" class:above hidden={typing}>{#if reviewVisible && reviewBar}{@render reviewBar(true)}{/if}</div>{/snippet}
{#snippet listBody()}
	{#if sheetDrawer}{@render rowHost(false)}{/if}
	<div class="drawer-heading"><h2 tabindex="-1" data-drawer-focus-home>Comments{#if headerCount}{' '}<span>{headerCount}</span>{/if}</h2><div class="drawer-tools">{#if image}<div class="filter-menu">
		<button type="button" class="filter-button" data-drawer-filter aria-haspopup="menu" aria-expanded={filterOpen} aria-label="Filter comments: {filterName}" onclick={() => filterOpen = !filterOpen}>Filter <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button>
		{#if filterOpen}<div class="filter-popup" role="menu" aria-label="Filter comments" in:menuPop={{ origin: 'top right' }} out:menuPop={{ origin: 'top right' }}>{#each FILTERS as [key, name] (key)}<button type="button" role="menuitemradio" aria-checked={filter === key} onclick={() => { filter = key; filterOpen = false; }}><span class="filter-check" aria-hidden="true">{#if filter === key}<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 5 5 9-10" /></svg>{/if}</span>{name}</button>{/each}</div>{/if}
	</div>{/if}<IconButton ariaLabel="Close comments" variant="ghost" size="sm" onclick={closeDrawer}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></IconButton></div></div>
	{#if load?.state === 'loading'}<p class="drawer-status">Loading comments…</p>{/if}
	{#if load?.state === 'error'}<div class="drawer-status drawer-error" role="status"><span>Couldn't load comments. Check the connection and retry.</span><Button size="sm" onclick={onRetry}>Retry</Button></div>{/if}
	{#if navigationNotice}<p class="drawer-status">{navigationNotice}</p>{/if}
	<!-- Accept all belongs to the column too, but takes no room unless there is something to say. -->
	{#if margin && !readOnly && (decisions.authorsWithOpenSuggestions.length || decisions.outcome.skipped.length || decisions.outcome.acceptAllError)}<div class="margin-accept-all">{@render acceptAllBar()}</div>{/if}
	{#if margin && (resolvedList.length || reviews.length)}<div class="margin-aside-entry" bind:this={aside.entry}>{#if resolvedList.length}<button type="button" class="filter-button aside-button" data-aside-kind="resolved" aria-expanded={aside.open && aside.kind === 'resolved'} onclick={() => { if (aside.open && aside.kind === 'resolved') aside.open = false; else { aside.kind = 'resolved'; aside.open = true; } }}>Resolved · {resolvedList.length} <svg class="aside-chevron" class:open={aside.open && aside.kind === 'resolved'} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button>{/if}{#if reviews.length}<button type="button" class="filter-button aside-button" data-aside-kind="review" aria-expanded={aside.open && aside.kind === 'review'} onclick={() => { if (aside.open && aside.kind === 'review') aside.open = false; else { aside.kind = 'review'; aside.open = true; } }}>{reviews.length} {reviews.length === 1 ? 'review comment' : 'review comments'} <svg class="aside-chevron" class:open={aside.open && aside.kind === 'review'} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg></button>{/if}</div>{/if}
	{#if margin && coversCards}<div class="margin-aside" style:top="{aside.top}px" style:max-height="calc(100% - {aside.top}px - var(--space-2))" role="region" aria-label={aside.kind === 'resolved' ? 'Resolved comments' : 'Review comments'} use:drawerKeys={aside.close} in:asidePop={{ origin: 'top left' }} out:asidePop={{ origin: 'top left' }}><div class="margin-aside-head"><h3>{aside.kind === 'resolved' ? 'Resolved' : 'Review comments'}</h3><IconButton ariaLabel="Close" variant="ghost" size="sm" onclick={aside.close}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></IconButton></div><div class="margin-aside-body">{@render threadList(aside.kind === 'resolved' ? resolvedList : reviews, true, true, false, true)}</div></div>{/if}
	<div class="drawer-comments" class:margin-clip={margin}>
		{#if image && image.showThumbnail !== false && displayed.some((comment) => comment.region)}<ImageRegionThumb src={image.src} alt={image.alt} comments={displayed} colorOf={image.colorOf} onOpen={image.onOpen} />{/if}
		<div class="peers">{#key view}<div class="peer" in:peerFade out:peerFade>
		{#if allShown.length && margin}<DocMarginStack {bounds} {pinned} {selectedId} behind={coversCards}>{#snippet card(comment)}{@render threadList([comment], true, true, true)}{/snippet}</DocMarginStack>{:else if displayed.length}{@render content()}{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">{comments.length === 0 ? 'No comments yet.' : view === 'open' ? 'No open comments.' : 'No resolved comments.'}{#if image && filter === 'resolved' && openCount}<button type="button" class="drawer-empty-action" onclick={() => filter = 'open'}>Show open · {openCount}</button>{/if}</p>{/if}
		{#if !margin && !image && resolvedList.length}{@render sheetResolved()}{/if}
		</div>{/key}</div>
	</div>
	<div class="drawer-footers" use:drawerFooterHeight>
		<CommentPlacementRecovery {...placementRecovery} onFocusHome={() => drawer?.querySelector<HTMLElement>('[data-drawer-focus-home]') ?? undefined} />
	</div>
{/snippet}

{#if surfaceState || sheet.closing}
	{#if margin && surfaceState}
		<section class="canvas-comment-drawer margin" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={escapeColumn} onclickcapture={(event) => opener = clickedOpener(event)} out:marginOut|global>{@render listBody()}</section>
	{:else if sheet.state?.kind === 'list' && sheetDrawer}
		<AdaptiveDrawer bind:panel={listSheet} ariaLabel="Document comments" initialFocusSelector={focusedIds ? '[data-comment-id].focused .ct-num' : '[data-drawer-autofocus]'} headless fill opaque {detent} onDetent={(next) => detent = next} followKeyboard={narrow} within={column} {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose}><section class="canvas-comment-drawer sheet" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)}>{@render listBody()}</section></AdaptiveDrawer>
	{:else if surfaceState?.kind === 'list'}
		<section class="canvas-comment-drawer" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:listDrawerShown use:drawerKeys={closeDrawer} onclickcapture={(event) => opener = clickedOpener(event)} in:drawerMotion|global={{ leaving: false }} out:drawerMotion|global={{ leaving: true }}>{@render listBody()}</section>
	{:else if cardAsSheet && sheet.state}<AdaptiveDrawer bind:panel={draftSheet} ariaLabel="Document comments" heading={sheet.state.kind === 'draft' ? 'New comment' : 'Comments'} doneLabel="Close" opaque followKeyboard {covered} open={sheet.open} onClose={sheet.close} onClosed={finishSheetClose} above={rowHost}>{@render content()}</AdaptiveDrawer>
	{:else if surfaceState}<Popover anchor={cardAnchor} placement={anchorAtClick ? 'pointer' : 'right'} {bounds} avoidRect={image?.imageBounds} initialFocus={image && !readOnly ? '.ct-input' : undefined} sideKey={surfaceState.kind === 'thread' ? surfaceState.ids?.[0] ?? surfaceState.id : anchor} open={true} {onClose} {restoreFocus} keepOpenOnPointer={(target) => !!image && surfaceState?.kind === 'draft' && target instanceof Element && !!bounds?.contains(target) && !!target.closest('[data-image-zoom]')}>{@render content()}</Popover>{/if}
{/if}

<style>
	/* Keep the review controls in the canvas container when the phone sheet is open. */
	.rb-sheet-host { container: doc-canvas / inline-size; padding: var(--space-2) 0 0; flex: none; overflow: hidden; scrollbar-gutter: stable; }
	.rb-sheet-host.above { margin: 0 2px 0 -4px; padding-top: 0; }
	.rb-sheet-host:empty, .rb-sheet-host[hidden] { display: none; }
	.canvas-comment-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; padding: var(--space-1) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
	/* The wide layout's cards take the Frosted card role through these tokens, which the thread card reads; every other caller of the card keeps its own. */
	/* Cards fill the clipped column edge to edge, so a shadow that spreads sideways is cut off square (a grey band under each card); a negative spread keeps the lift below the card only. */
	.canvas-comment-drawer.margin { --ct-bg: var(--glass-thin); --ct-border: var(--border); --ct-radius: var(--radius-xl); --ct-shadow: var(--glass-rim), 0 10px 14px -10px color-mix(in srgb, black 22%, transparent); position: relative; border-top: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-3); overflow: hidden; }
	/* The column grows in from nothing; its content keeps the final width so the reply fields measure at it. */
	.canvas-comment-drawer.margin > :global(*) { min-width: 287px; }
	/* `clip`, not `hidden`: the column never scrolls on its own (the cards follow the text), and a hidden box still scrolls when focus or a click moves into a card below its edge, which would carry every card away from its line. */
	.drawer-comments.margin-clip { position: relative; overflow: clip; scrollbar-gutter: auto; }
	.sheet-resolved { display: flex; flex-direction: column; gap: var(--space-1); padding-top: var(--space-1); }
	.margin-aside-entry { flex: none; display: flex; flex-direction: column; align-items: flex-start; padding: 0 0 var(--space-1) 0; }
	/* The entry is the column's own Filter control's kind (a text control with a chevron that opens a list); it darkens on hover. */
	.aside-button { max-width: 100%; padding: 0 var(--space-1) 0 4px; color: var(--text-secondary); white-space: nowrap; transition: color var(--dur-tight) var(--ease-snap), background-color var(--dur-tight) var(--ease-snap); }
	.aside-button:hover { color: var(--text-primary); background: var(--bg-hover); }
	.aside-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.aside-chevron { flex: none; transition: transform var(--dur-tight) var(--ease-snap); }
	.aside-chevron.open { transform: rotate(180deg); }
	/* The list fits its content up to the column's height under its entry, then scrolls; its head, with the close, stays put. It is opaque, so no card behind it shows partly: it is the filter popup's surface. */
	.margin-aside { position: absolute; z-index: 2; left: var(--space-3); right: var(--space-2); display: flex; flex-direction: column; gap: var(--space-2); overflow: hidden; padding: var(--space-2); border: 1px solid var(--border); border-radius: var(--radius-xl); background: var(--bg-elevated); box-shadow: var(--glass-rim), var(--shadow-chip); }
	.margin-aside-body { min-height: 0; overflow: auto; overscroll-behavior: contain; }
	.margin-aside-head { flex: none; display: flex; align-items: center; justify-content: space-between; }
	.margin-aside-head h3 { margin: 0; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.canvas-comment-drawer.sheet { flex: 1; padding: 0; border-top: 0; }
	@container doc-canvas (min-width: 900px) { .canvas-comment-drawer { border-top: 0; border-left: 1px solid var(--border-subtle); padding-left: var(--space-3); } }
	.drawer-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 0 var(--space-1) 4px; }
	.drawer-heading h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	/* Where focus rests when a pinned footer takes it away: not a control, so Enter there does nothing. */
	.drawer-heading h2:focus { outline: none; }
	.drawer-heading h2:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-xs); }
	.drawer-heading h2 span { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
	/* The compact menu keeps the filter choices out of the thread list. */
	.drawer-tools { display: inline-flex; align-items: center; gap: var(--space-1); }
	.filter-menu { position: relative; }
	.filter-button, .filter-popup button { min-height: var(--space-6); border: 0; border-radius: var(--radius-xs); background: none; color: var(--text-secondary); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
	.filter-button { display: inline-flex; align-items: center; gap: 4px; padding: 0 var(--space-2); }
	.filter-popup { position: absolute; z-index: 2; right: 0; top: calc(100% + var(--space-1)); display: grid; min-width: 9rem; padding: var(--space-1); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-elevated); box-shadow: var(--shadow-chip); }
	.filter-popup button { display: flex; align-items: center; gap: var(--space-1); padding: 0 var(--space-2); text-align: left; }
	.filter-check { display: inline-flex; flex: none; width: 12px; height: 12px; }
	.filter-popup button[aria-checked="true"] { color: var(--accent-text); font-weight: var(--fw-semibold); }
	@media (pointer: coarse) { .drawer-heading :global(button), .filter-button, .filter-popup button { min-height: 44px; } .drawer-heading :global(.ico-btn) { min-width: 44px; } }
	/* A filter switch crossfades: the leaving and arriving lists share one grid cell, so neither moves the other. */
	.peers { display: grid; }
	.peer { grid-area: 1 / 1; min-width: 0; }
	.drawer-comments { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
	.drawer-status { color: var(--text-secondary); font-size: var(--text-footnote); margin: 4px 0; }
	/* Retry sits on its own row under the sentence, so it never touches the end of a wrapped line. */
	.drawer-error { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-2); }
	.drawer-empty-action { margin-left: var(--space-1); padding: 0; border: 0; background: none; color: var(--accent-text); font: inherit; font-weight: var(--fw-semibold); cursor: pointer; }
	.drawer-empty-action:hover { text-decoration: underline; }
	/* The one measured wrapper owns the drawer's pinned-footer height, including either or both child footers. */
	.drawer-footers { flex: none; }
	.drawer-footers > :not(:last-child) { border-radius: 0; }
	.margin-accept-all { flex: none; padding: 0 0 var(--space-1) 0; }
</style>
