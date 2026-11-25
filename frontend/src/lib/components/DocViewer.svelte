<script lang="ts">
	import { fade } from 'svelte/transition';
	import type { ChangeDesc } from '@codemirror/state';
	import { cubicOut } from 'svelte/easing';
	import './DocViewer.css';
	import { docCanvasBounds } from '$lib/actions/docCanvasBounds';
	import { isMobile, isShortTouch, prefersReducedMotion } from '$lib/stores/viewport';
	import { onDestroy, untrack } from 'svelte';
	import { ensureEditorArtifactId, releaseDocEditor } from '$lib/stores/activeDocEditor';
	import { docComments, commentLoads, remapComments, markThreadViewed, hasUnread, pickedComments } from '$lib/stores/docComments';
	import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import { focusedDocComment } from '$lib/stores/docCommentFocus';
	import { commentDraft, commentDraftScope } from '$lib/stores/commentDraft';
	import { createViewerCommentDraft } from '$lib/docViewerCommentDraft.svelte';
	import { createCommentScrollTarget } from '$lib/docViewerScrollTarget.svelte';
	import { createPressReveal } from '$lib/docPressReveal.svelte';
	import { createCommentOpeners } from '$lib/docViewerClickPoint';
	import { createHeldUnread, openCommentIds } from '$lib/docHeldUnread.svelte';
	import { createReplyNavigation } from '$lib/docReplyLanding.svelte';
	import { createDocViewerImages } from '$lib/docViewerImages';
	import { projects } from '$lib/stores/projects';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import DocHistorySurface from './DocHistorySurface.svelte';
	import { createViewerHistory } from '$lib/docViewerHistory.svelte';
	import type { DocOpenRefusal } from '$lib/docOpenRefusal';
	import { loadCurrentViewer } from '$lib/docViewerContentLoad';
	import { applyDocumentContent } from '$lib/docViewerApplyContent';
	import { bindViewerSession, reloadAfterLegacySave, reopenOnReconnect } from '$lib/docViewerLiveBinding.svelte';
	import { bindViewerDraft } from '$lib/docViewerDraftBinding.svelte';
	import { createThreadActions, prepareDocumentReload, reloadDocumentFeedback, teardownDocumentViewer, verifyAndApplyDocumentFreshness } from '$lib/docViewerOperations';
	import { closeCardPopover, handleDocumentKey, type DocumentKeyState } from '$lib/docViewerKeyboard';
	import { acceptAllOpen, openSuggestionCount } from '$lib/docAcceptAll';
	import { closeCellUnderSheet, createCommentSurfaceState, focusDocumentEditor, openColumnOnLoad } from '$lib/docCommentSurfaceState.svelte';
	import { awaitsFirstMark, canvasCommentHighlights, canvasCommentScrollTarget, commentSurfaceStatus, commentsInDocumentOrder } from '$lib/docCanvasComments';
	import { leavePreviousTarget, type ViewerContent } from '$lib/docViewerLoading';
	import { liveDocumentBindings } from '$lib/stores/liveDocuments';
	import { connectionState } from '$lib/stores/connection';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import { syncActiveDocumentEditor } from '$lib/docViewerEditorState';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { DocState, DocViewerProps } from '$lib/docViewerProps';
	import { createViewerHeader } from '$lib/docViewerHeader.svelte';
	import { createDocSaveState } from '$lib/docViewerSaveState.svelte';
	import { reload as reloadAttentionQueue } from '$lib/stores/attentionDock.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import DocViewerEditorSurface from './DocViewerEditorSurface.svelte';
	import DocPresenceStatus, { type PresenceView } from './DocPresenceStatus.svelte';
	import DocPresenceAvatars from './DocPresenceAvatars.svelte';
	import DocCanvasActivity from './DocCanvasActivity.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import DocViewerStatus from './DocViewerStatus.svelte';
	import ReviewBar from './ReviewBar.svelte';
	import { createReviewBarState, documentAskOf } from '$lib/reviewBarState.svelte';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import DocumentViewport from './DocumentViewport.svelte';
	import { createDocImageCacheKey } from '$lib/docImageCacheKey.svelte';
	import { createDocumentReading } from '$lib/docViewerReading.svelte';
	import { createThreadHomes } from './comments/threadHomes';

	let {
		target,
		onClose = () => {},
		escapeCloses = true,
		onSignal,
		stale = false,
		onReload,
		onOpenImage,
		onDocumentPlace,
		covered = false,
		offScreen = false,
		onLanded,
	}: DocViewerProps = $props();

	let docState = $state<DocState>('loading');
	let openRefusal = $state<DocOpenRefusal | null>(null);
	let liveUnavailable = $state<ViewerContent['liveUnavailable']>(null);
	let loadedArtifactId = $state<string | null>(null);
	let readonly = $state(false);
	let confirming = $state(false);
	let serverReadOnly = $state(false); // Only the loaded content's own answer: not unsaved text awaiting its write check, and not an open refusal.
	const header = createViewerHeader(() => ({ target, artifactId: loadedArtifactId }));
	const focusedComment = $derived($focusedDocComment);

	let editText = $state('');
	let suggestionBusyId = $state<string | null>(null);
	let feedbackStatus = $state<DocViewerStatus>();

	const save = createDocSaveState(() => target);
	const history = createViewerHistory({ key: () => targetKey, target: () => target, closeComments: () => commentUI.closeAll(), columnShown: () => (commentUI.state?.kind === 'list' || commentUI.state?.kind === 'thread') && columnFits(), showColumn: () => { if (listedComments.length > 0 && columnFits()) commentUI.openOnLoad(); }, root: () => docRoot, liveText: () => liveSession?.text.toString() ?? null });
	const liveSession = $derived(target ? $liveDocumentBindings[target.instanceId] : undefined);
	let liveConnection = $state<LiveDocumentSession['connection'] | null>(null);
	let liveGeneration = $state(0);
	let legacyAwaitingSave = $state(false);
	bindViewerSession(() => liveSession, (state) => {
		if (!state) { liveConnection = null; return; }
		editText = state.text.toString();
		readonly = !state.writable; serverReadOnly = !state.writable;
		liveConnection = state.connection;
		liveGeneration = state.textGeneration;
	});

	let presenceRanges = $state<PresenceRange[]>([]);
	let presenceView = $state<PresenceView | null>(null);
	let activity = $state<DocCanvasActivity>();
	const imageKey = createDocImageCacheKey(() => target?.threadId, () => loadedArtifactId);
	// The comments pane derives orphaning from the text currently on screen (and a refused accept)
	// so a comment never remains highlighted after an in-editor edit changes it.
	$effect(() => syncActiveDocumentEditor({ target, draft: save.draft, docState, artifactId: loadedArtifactId, text: editText, readonly: readonly || confirming }));

	const listedComments = $derived(commentsInDocumentOrder(loadedArtifactId ? ($docComments[loadedArtifactId] ?? []) : [], editText, loadedArtifactId ? $suggestionRefusals[loadedArtifactId] : undefined, suggestionBusyId));
	// The viewer is not rebuilt per document: a card that closes as another document loads is still read in its own.
	const homes = createThreadHomes();
	$effect.pre(() => homes.note((loadedArtifactId ? $docComments[loadedArtifactId] ?? [] : []).map((comment) => comment.id), loadedArtifactId));
	const commentStatus = $derived(commentSurfaceStatus(listedComments));
	const unreadThreads = $derived(listedComments.filter(hasUnread));
	const scroll = createCommentScrollTarget({
		focused: () => focusedComment, comments: () => listedComments, text: () => editText, listOpen: () => commentUI.state?.kind === 'list',
	});
	let docRoot = $state<HTMLElement>();
	let editorSurface = $state<ReturnType<typeof DocViewerEditorSurface>>();
	/** The comments drawer's selected view: resolved threads shown or hidden. The count is the surface's own. */
	let resolvedOpen = $state(false);
	let commentSurface = $state<ReturnType<typeof DocCommentSurface>>();
	const typingAvailable = () => docState === 'ready' && !readonly && !confirming && !openRefusal && suggestionBusyId === null;
	// A primitive, for the reason targetKey is one: a revision refresh replaces the `target` object with the same id, and that must not end Edit.
	const openingId = $derived(target?.instanceId ?? null);
	const reading = createDocumentReading({ opening: () => openingId, offScreen: () => offScreen && !covered, available: typingAvailable,
		clearSelection: () => { editorSurface?.clearInputSelection(); draftController.select(null); } });

	// Whether the margin column is up is the surface's own answer, the one its cards and column follow.
	const columnFits = () => commentSurface?.cardMode() === 'margin';
	const commentUI = createCommentSurfaceState(() => focusDocumentEditor(docRoot), columnFits);
	const reviewBarState = createReviewBarState();
	/** The ask waiting on this document, for Details while its card is hidden (under a comment sheet, or stepped aside for Details). */
	const askSheetHidden = $derived(commentUI.state != null && commentUI.sheetUp);
	const documentAsk = $derived(documentAskOf(reviewBarState, askSheetHidden));
	closeCellUnderSheet(commentUI, () => docRoot);
	// Below the margin width a card opened from the text is a popover over it: the caret leaving or a key typed in the text closes it, so it never covers what is being written.
	const closeTextCard = () => { if (commentSurface?.cardMode() === 'popover' && commentUI.state?.kind === 'thread' && commentUI.state.fromText) commentUI.close(); };
	// The text shows the hovered thread's mark focused; its card only wears the focused ring (`ringId`). The pick alone opens,
	// places and reads a card, so a passing pointer neither holds a New mark nor moves the margin nor takes the writer turn.
	const shownFocus = $derived(commentUI.hovered ?? focusedComment);
	// The list over the text, as the sheet or (below the margin width) the popover: the selection's Comment button waits for it to close.
	const listOverText = $derived(commentUI.sheetUp || (commentUI.state?.kind === 'list' && commentSurface?.cardMode() === 'popover'));
	openColumnOnLoad({ artifactId: () => loadedArtifactId, ready: () => docState === 'ready' && !!loadedArtifactId && $commentLoads[loadedArtifactId]?.state === 'ready', paneDrawer: isPaneDrawer, fits: columnFits, hasComments: () => listedComments.length > 0, shown: () => !!commentUI.state, open: commentUI.openOnLoad, quietList: () => commentUI.state?.kind === 'list' && commentUI.quiet, close: commentUI.closeAll });
	const heldUnread = createHeldUnread(() => openCommentIds(commentUI.state, focusedComment), () => listedComments);
	const highlights = $derived(canvasCommentHighlights(listedComments, editText, shownFocus, focusMarker, suggestionBusyId, (comment) => images.groups.colorOf(comment)).map(heldUnread.paint));
	const openCards = createCommentOpeners(commentUI, scroll);
	const { notePress, revealCard, revealDraft } = createPressReveal({ highlights: () => highlights, view: () => editorSurface?.editorView(), coveredBySheet: (from, opening) => editorSurface?.coveredBySheet(from, opening), scrollTo: (to) => { scroll.value = to; }, draft: () => draftController.draft, sheetUp: () => commentUI.sheetUp });
	// A marker over comments on parts of one image opens the image, at the comment when it is the only one (DCOL-FR13.5).
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null, click?: MouseEvent, fromText = false) { notePress(ids, click); pickedComments.set(new Set(ids)); images.openOr(ids, ids.length === 1 ? { id: ids[0] } : undefined, () => openCards.markers(ids, opener, reading, click, fromText)); }
	function onDocEdit(changes: ChangeDesc, open: ReadonlySet<string>) { if (loadedArtifactId) remapComments(loadedArtifactId, changes, open); images.remap((p) => changes.mapPos(p)); }

	const accent = $derived($projects.find((project) => project.id === target?.projectId)?.accent_color ?? null);
	const images = createDocViewerImages({
		onOpenImage: () => onOpenImage, root: () => docRoot, comments: () => listedComments, artifactId: () => loadedArtifactId,
		target: () => target, readOnly: () => readonly || confirming, text: () => editText, session: () => liveSession, accent: () => accent,
		feedback: () => feedbackStatus?.feedback(), imageCacheKey: () => imageKey.current, showInText: focusInText });
	/** The comments of the image shown over this document, bound to it as it is now: an image's host rebuilds them when it shows one. */
	export function imageComments() { return images.comments(); }
	function focusInText(id: string, placement?: 'top') { scroll.toComment(id, commentUI.state?.kind === 'list', placement === 'top' ? 'quote-tap' : 'navigation'); commentUI.selectMarkerThread(id); focusedDocComment.set(id); }
	const replyNavigation = createReplyNavigation({
		target: () => target, ready: () => !!loadedArtifactId && $commentLoads[loadedArtifactId]?.state === 'ready' && docState === 'ready',
		comments: () => listedComments, text: () => editText,
		show: (thread, landing, picked) => { if (thread && landing) scroll.value = landing; if (thread) focusedDocComment.set(thread.id); resolvedOpen = !!thread?.resolved; commentUI.state = { kind: 'list' }; if (thread && picked) pickedComments.set(new Set([thread.id])); },
		openImageOr: (id, otherwise) => { images.openOr([id], { id }, otherwise); pickedComments.set(new Set([id])); }, onLanded: () => onLanded?.(),
	});
	export function jumpToUnread(id: string, beforeText?: () => void) { replyNavigation.goTo(id, beforeText); }
	// A card for a comment on part of an image opens the image at its tag; any other card goes to its text.
	function focusFromCard(id: string, placement?: 'top') { commentUI.engage(); images.openOr([id], { id }, () => focusInText(id, placement)); }

	const draftController = createViewerCommentDraft({
		scope: () => targetKey, target: () => target, draft: () => $commentDraft, session: () => liveSession,
		generation: () => liveGeneration, text: () => editText, isOpen: () => commentUI.state?.kind === 'draft',
		ensureArtifactId: ensureEditorArtifactId,
		onStart: commentUI.openDraft, onClose: commentUI.close,
		awaitsMark: (id) => awaitsFirstMark(listedComments.find((item) => item.id === id), highlights.some((range) => range.id === id)),
	});
	async function submitComment(text: string, taggedAgentIds: string[], mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[]) {
		const id = await draftController.submit(text, taggedAgentIds, target?.threadId ?? null, mentions);
		if (!id) return;
		scroll.toComment(id, true, 'comment-save');
		focusedDocComment.set(id);
		commentUI.state = { kind: 'thread', id };
		commentDraft.set(null);
		feedbackStatus?.commentCreated();
	}
	const threadActions = createThreadActions(() => loadedArtifactId, () => feedbackStatus?.commentCreated(), () => target?.threadId ?? null);

	// Keyed off the load-relevant fields only (not object identity) so an
	// unrelated re-render that recreates the `target` prop object (e.g. a
	// sibling tab's status update) never re-triggers a reload.
	const targetKey = $derived(commentDraftScope(target));

	let prevTarget: DocViewerTarget | null = null;
	let reloadNonce = $state(0);

	export function reload() {
		if (liveSession) reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus);
		else reloadNonce += 1;
	}

	let loadSeq = 0;
	function applyViewerContent(d: DocViewerTarget, content: ViewerContent, seq: number): void {
		applyDocumentContent(d, content, seq, { loadSeq: () => loadSeq, shown: () => docState === 'ready', signal: onSignal,
			feedback: () => feedbackStatus, apply: (value, awaiting) => {
				editText = value.text; loadedArtifactId = value.artifactId; readonly = !value.writable; serverReadOnly = !value.writable; confirming = false; history.setAvailable(value.history === true);
				header.loaded(d, value); docState = 'ready'; legacyAwaitingSave = awaiting;
			},
		});
	}
	reloadAfterLegacySave(() => legacyAwaitingSave, () => save.draft?.status === 'saved', () => { legacyAwaitingSave = false; }, () => { reloadNonce += 1; });
	reopenOnReconnect(() => $connectionState === 'connected', () => openRefusal?.code === 'not_connected', () => { reloadNonce += 1; });
	const applyLoaded = (content: ViewerContent) => { if (target && !liveSession) applyViewerContent(target, content, ++loadSeq); };
	function onPrepareCleanReload(): Promise<(() => void) | null> {
		const seq = loadSeq;
		return target && !liveSession ? prepareDocumentReload(target, () => seq === loadSeq, applyLoaded) : Promise.resolve(null);
	}
	$effect(() => {
		const key = targetKey;
		reloadNonce;
		if (!key) return;
		untrack(() => {
			const d = target;
			if (!d) return;
			const seq = ++loadSeq;
			// Only the previous target's comment draft is dropped, and what was typed in it is kept (#3088).
			draftController.leave(prevTarget, key);
			prevTarget = leavePreviousTarget(prevTarget, d);
			commentUI.closeAll();
			loadCurrentViewer({ target: d, isCurrent: () => seq === loadSeq, onSignal,
				begin: (nextId, opened) => {
					// These fields describe the mounted target, not its predecessor; a reload of the same file keeps the server's known answer.
					if (nextId !== loadedArtifactId) serverReadOnly = false;
					loadedArtifactId = nextId; readonly = opened.unsaved; confirming = opened.confirming;
					if (opened.text !== null) editText = opened.text;
					header.begin(d); openRefusal = null; liveUnavailable = null;
					docState = opened.text === null ? 'loading' : 'ready';
				},
				ready: (content) => { liveUnavailable = content.liveUnavailable; applyViewerContent(d, content, seq); },
				snapshot: (text) => { editText = text; },
				failure: (failure) => { openRefusal = failure.refusal; docState = failure.state;
					if (failure.text !== null) editText = failure.text; readonly = failure.readonly; confirming = false; },
				reloadAttention: () => { void reloadAttentionQueue(); },
			});
		});
	});

	const flushNow = bindViewerDraft({ target: () => target, text: () => editText,
		canEdit: () => docState === 'ready' && !readonly && !confirming && !openRefusal,
		canFlush: () => !readonly && !confirming });

	onDestroy(() => { teardownDocumentViewer(target, flushNow, releaseDocEditor); });

	const keyState = (): DocumentKeyState => ({ covered, commentUI, draftController, cardMode: commentSurface?.cardMode(), paneOpen: $paneOpen, isPaneDrawer: isPaneDrawer(), escapeCloses, flush: flushNow, close: onClose });
</script>

<svelte:window onkeydown={(event) => handleDocumentKey(event, keyState())} onkeydowncapture={(event) => closeCardPopover(event, keyState())} />
<DocumentViewport instanceId={target?.instanceId} ready={docState === 'ready'} root={docRoot} />
<div class="doc" class:margin-room={commentSurface?.marginShown()} use:docCanvasBounds bind:this={docRoot} in:fade={{ duration: $prefersReducedMotion ? 0 : 160, easing: cubicOut }}>
	{#if target && loadedArtifactId}
		<DocPresenceStatus {target} artifactId={loadedArtifactId} root={docRoot}
			textCurrent={liveConnection === null || liveConnection === 'connected' || liveConnection === 'refused'}
			projectComment={(comment) => commentsInDocumentOrder([comment], editText, $suggestionRefusals[loadedArtifactId!], suggestionBusyId)[0]}
			bind:ranges={presenceRanges} bind:presenceView {onPrepareCleanReload}
			onArrival={(frame) => void activity?.arrive(frame)}
			onFeedback={(id, feedback) => feedbackStatus?.sync(id, feedback)} />
	{/if}
	<DocViewerStatus
		touchReading={reading.touch} editing={reading.editing} canEdit={typingAvailable()} onEnterEdit={reading.enter} onDone={reading.finish}
		commentTaskBar={reviewBarState}
		ask={documentAsk}
		commentsOpen={commentUI.state?.kind === 'list'}
		commentsLoading={docState === 'loading' || (loadedArtifactId ? $commentLoads[loadedArtifactId]?.state === 'loading' : false)}
		saveStatus={history.restoring ? 'saving' : save.status}
		bind:this={feedbackStatus}
		ready={docState === 'ready'}
		stale={stale && !liveSession}
		readonly={readonly && !confirming && !openRefusal}
		commentCount={commentStatus.commentCount}
		unreadCount={unreadThreads.length}
		artifactId={loadedArtifactId}
		viewingThreadId={target?.threadId ?? null}
		reviewTargetKey={targetKey}
		targetInstanceId={target?.instanceId ?? null}
		imageOpen={covered} {offScreen} activityRoot={docRoot}
		{onReload}
		onOpenComments={(opener) => { history.close(); commentUI.openList(opener); }}
		historyAvailable={history.available}
		historyOpen={history.open}
		onOpenHistory={history.toggle}
		freshness={header.freshness} ownership={header.ownership} headerStandIn={header.standIn} onOwnershipChanged={header.settling(target, loadedArtifactId).ownership}
		onFeedbackChanged={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onMarkVerified={() => verifyAndApplyDocumentFreshness(loadedArtifactId, header.freshness, header.settling(target, loadedArtifactId).freshness)}
		liveSession={liveSession ?? null}
		{openRefusal} {liveUnavailable}
		tabText={editText}
		onReopen={() => reloadNonce += 1}
		onRead={() => reloadNonce += 1}
	>
		{#snippet reviewBar()}{@render reviewControls()}{/snippet}
		{#snippet presence()}<DocPresenceAvatars view={presenceView} artifactId={loadedArtifactId} instanceId={target?.instanceId} ready={docState === 'ready'} />{/snippet}
	</DocViewerStatus>
	{#if target}<DocBottomBand artifactId={loadedArtifactId} threads={unreadThreads} root={docRoot} ready={docState === 'ready'} {covered} sheetModalOpen={commentUI.sheetUp} onJump={(thread) => { commentUI.restoreReplyFocus(() => docRoot); replyNavigation.goTo(thread.id); }} onControlGone={() => focusDocumentEditor(docRoot)} />{/if}
	<DocCanvasActivity bind:this={activity} root={docRoot} artifactId={loadedArtifactId} threadId={target?.threadId ?? null} onJump={(span) => scroll.value = { ...span, origin: 'navigation' }} />
	<DocViewerEditorSurface
		bind:this={editorSurface}
		artifactId={loadedArtifactId}
		{docState}
		documentName={target?.name ?? 'this document'}
		onRetry={() => reloadNonce += 1}
		bind:value={editText}
		liveText={liveSession?.text ?? null} typingAllowed={liveConnection === 'connected'} previews={liveSession ?? null}
		{highlights}
		presence={presenceRanges}
		pendingComment={draftController.pendingComment}
		scrollTo={scroll.value}
		onSave={flushNow}
		onSelect={draftController.select}
		onAnchorFocus={(id, anchor, viaSuggestion, click) => { notePress([id], click); openCards.comment(id, anchor, viaSuggestion, click); }}
		onCaretAway={() => { if (!commentUI.release()) closeTextCard(); }}
		onTyping={closeTextCard}
		cardMode={commentSurface?.cardMode()}
		onSuggestionHover={commentUI.hover}
		onEdit={onDocEdit}
		onLayout={(mapPos) => activity?.editorChanged(mapPos)}
		reading={reading.reading} touchReading={reading.touch}
		readOnly={readonly || confirming || suggestionBusyId !== null}
		imageBase={target && { ...target, imageCacheKey: imageKey.current }}
		onOpenImage={onOpenImage ? (request) => images.open(request) : undefined}
		tabInstanceId={target?.instanceId ?? null}
		{onDocumentPlace}
		saveError={save.error}
		onUseFile={applyLoaded}
		pendingSel={draftController.pending}
		{listOverText}
		onStartComment={draftController.start}
	/>
	{#if history.open && history.available && target}<DocHistorySurface {target} readOnly={serverReadOnly} applyRestore={history.restore} onRestored={async () => { (await onPrepareCleanReload())?.(); }} bind:restoring={() => history.restoring, history.setRestoring} narrow={isPaneDrawer()} within={docRoot?.closest<HTMLElement>('.leaf-body') ?? docRoot} {covered} onClose={history.close} />{/if}
	<DocCommentSurface
		bind:this={commentSurface}
		state={commentUI.state}
		anchor={commentUI.anchor}
		anchorAtClick={commentUI.atClick}
		bounds={docRoot}
		commentsEntry={feedbackStatus?.commentsButton()}
		feedback={feedbackStatus?.feedback()}
		narrow={isPaneDrawer()}
		phone={$isMobile || $isShortTouch}
		onRelease={commentUI.release}
		quiet={commentUI.quiet}
		comments={listedComments}
		widgetLabel={{ read: (from, to, quote) => editorSurface?.widgetLabel(from, to, quote), stamp: () => editorSurface?.widgetLabelStamp() }}
		focusedId={focusedComment}
		ringId={commentUI.hovered}
		draft={draftController.draft}
		draftReason={draftController.reason}
		onDraftBody={draftController.setBody}
		onCancelDraft={draftController.cancel}
		onClose={commentUI.close}
		onThreadMissing={commentUI.focusRemainingThread}
		restoreFocus={commentUI.restoreFocus}
		onFocus={focusFromCard}
		load={loadedArtifactId ? $commentLoads[loadedArtifactId] : undefined}
		onRetry={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onSubmit={submitComment}
		onReply={threadActions.reply}
		onViewed={(id, replyId) => homes.stamp(id, (home) => markThreadViewed(home, id, replyId))}
		onResolve={threadActions.resolve}
		onAgree={threadActions.agree}
		{target}
		artifactId={loadedArtifactId}
		draftScope={draftController.keepKey}
		readOnly={readonly || confirming}
		onSuggestionBusy={(busy, id) => suggestionBusyId = busy ? id ?? '' : null}
		onSuggestionAccepted={(text) => { if (!liveSession) editText = text; }}
		onDocumentChanged={reload}
		onSheetModalChange={(open) => commentUI.sheetUp = open}
		onRevealDraft={revealDraft} onRevealCard={revealCard}
		images={images.groups}
		{covered}
		{offScreen}
		draftOverColumn={commentUI.draftOverColumn}
		opened={commentUI.opened}
		bind:resolvedOpen
	/>
</div>
{#snippet reviewControls()}
	<ReviewBar docked={$isMobile || $isShortTouch} artifactId={loadedArtifactId} bar={reviewBarState} draftOpen={draftController.hasText} suppressed={askSheetHidden} pendingSuggestions={readonly || confirming ? 0 : openSuggestionCount(listedComments)} onAcceptAll={async () => { suggestionBusyId = ''; try { return await acceptAllOpen(target, loadedArtifactId ?? '', { onText: (text) => { if (!liveSession) editText = text; }, onDocumentChanged: reload }); } finally { suggestionBusyId = null; } }} onAnswered={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)} />
{/snippet}
<!-- Styles are in DocViewer.css. -->
