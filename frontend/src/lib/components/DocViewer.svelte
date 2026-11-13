<script lang="ts">
	import { fade } from 'svelte/transition';
	import type { ChangeDesc } from '@codemirror/state';
	import { cubicOut } from 'svelte/easing';
	import './DocViewer.css';
	import { docCanvasBounds } from '$lib/actions/docCanvasBounds';
	import { isMobile, prefersReducedMotion } from '$lib/stores/viewport';
	import { onDestroy, untrack } from 'svelte';
	import { ensureEditorArtifactId, releaseDocEditor } from '$lib/stores/activeDocEditor';
	import { docComments, commentLoads, remapComments, markThreadViewed, hasUnread, arrivedDocComments } from '$lib/stores/docComments';
	import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import { focusedDocComment } from '$lib/stores/docCommentFocus';
	import { commentDraft, commentDraftScope, discardCommentDraft, setCommentBody } from '$lib/stores/commentDraft';
	import { createViewerCommentDraft } from '$lib/docViewerCommentDraft.svelte';
	import { createCommentScrollTarget } from '$lib/docViewerScrollTarget.svelte';
	import { createCommentOpeners } from '$lib/docViewerClickPoint';
	import { createHeldUnread } from '$lib/docHeldUnread.svelte';
	import { createReplyNavigation } from '$lib/docReplyLanding.svelte';
	import { createDocViewerImages } from '$lib/docViewerImages';
	import { projects } from '$lib/stores/projects';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import DocHistorySurface from './DocHistorySurface.svelte';
	import { createViewerHistory } from '$lib/docViewerHistory.svelte';
	import type { DocOpenRefusal } from '$lib/docOpenRefusal';
	import { handleViewerFailure } from '$lib/docViewerFailure';
	import { applyDocumentContent } from '$lib/docViewerApplyContent';
	import { bindViewerSession, reopenOnReconnect } from '$lib/docViewerLiveBinding.svelte';
	import { bindViewerDraft } from '$lib/docViewerDraftBinding.svelte';
	import type { DocumentFreshness } from '$lib/docFreshness';
	import { createCommentRetryToast, createThreadActions, prepareDocumentReload, reloadDocumentFeedback, teardownDocumentViewer, verifyAndApplyDocumentFreshness } from '$lib/docViewerOperations';
	import { closeCardPopover, handleDocumentKey, type DocumentKeyState } from '$lib/docViewerKeyboard';
	import { acceptAllOpen, openSuggestionCount } from '$lib/docAcceptAll';
	import { createCommentSurfaceState, focusDocumentEditor, marginFits, openColumnOnLoad } from '$lib/docCommentSurfaceState.svelte';
	import { awaitsFirstMark, canvasCommentHighlights, commentSurfaceStatus, commentsInDocumentOrder } from '$lib/docCanvasComments';
	import { leavePreviousTarget, loadViewerContent, openFromDraft, type ViewerContent } from '$lib/docViewerLoading';
	import { liveDocumentBindings } from '$lib/stores/liveDocuments';
	import { connectionState } from '$lib/stores/connection';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import { syncActiveDocumentEditor } from '$lib/docViewerEditorState';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { DocViewerProps } from '$lib/docViewerProps';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import {
		withDraftArtifactId, existingDraft,
	} from '$lib/stores/docSaveQueue';
	import { createDocSaveState } from '$lib/docViewerSaveState.svelte';
	import { reload as reloadAttentionQueue } from '$lib/stores/attentionDock.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import DocViewerEditorSurface from './DocViewerEditorSurface.svelte';
	import DocPresenceStatus from './DocPresenceStatus.svelte';
	import DocCanvasActivity from './DocCanvasActivity.svelte';
	import DocBottomBand from './DocBottomBand.svelte';
	import DocViewerStatus from './DocViewerStatus.svelte';
	import ReviewBar from './ReviewBar.svelte';
	import { createReviewBarState } from '$lib/reviewBarState.svelte';
	import { toasts } from '$lib/stores/toast';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import DocumentViewport from './DocumentViewport.svelte';
	import { createDocImageCacheKey } from '$lib/docImageCacheKey.svelte';

	type DocState = 'loading' | 'ready' | 'error';


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
		readStampSuppressed = false,
		onLanded,
	}: DocViewerProps = $props();

	let docState = $state<DocState>('loading');
	let openRefusal = $state<DocOpenRefusal | null>(null);
	let liveUnavailable = $state<ViewerContent['liveUnavailable']>(null);
	let loadedArtifactId = $state<string | null>(null);
	let readonly = $state(false);
	let confirming = $state(false);
	let serverReadOnly = $state(false); // Only the loaded content's own answer: not unsaved text awaiting its write check, and not an open refusal.
	let freshness = $state<DocumentFreshness | null>(null);
	let ownership = $state<import('$lib/api/docOwnership').DocOwnership | null>(null);
	const focusedComment = $derived($focusedDocComment);

	let editText = $state('');
	let suggestionBusyId = $state<string | null>(null);
	let feedbackStatus = $state<DocViewerStatus>();

	const save = createDocSaveState(() => target);
	const history = createViewerHistory({ key: () => targetKey, target: () => target, closeComments: () => commentUI.closeAll(), columnShown: () => (commentUI.state?.kind === 'list' || commentUI.state?.kind === 'thread') && marginFits(docRoot, isPaneDrawer()), showColumn: () => { if (listedComments.length > 0 && marginFits(docRoot, isPaneDrawer())) commentUI.openOnLoad(); }, root: () => docRoot, liveText: () => liveSession?.text.toString() ?? null });
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

	const commentRetryToast = createCommentRetryToast(toasts);
	let presenceRanges = $state<PresenceRange[]>([]);
	let activity = $state<DocCanvasActivity>();
	const imageKey = createDocImageCacheKey(() => target?.threadId, () => loadedArtifactId);
	// The comments pane derives orphaning from the text currently on screen (and a refused accept)
	// so a comment never remains highlighted after an in-editor edit changes it.
	$effect(() => syncActiveDocumentEditor({ target, draft: save.draft, docState, artifactId: loadedArtifactId, text: editText, readonly: readonly || confirming }));

	const listedComments = $derived(commentsInDocumentOrder(loadedArtifactId ? ($docComments[loadedArtifactId] ?? []) : [], editText, loadedArtifactId ? $suggestionRefusals[loadedArtifactId] : undefined, suggestionBusyId));
	const commentStatus = $derived(commentSurfaceStatus(listedComments));
	const unreadThreads = $derived(listedComments.filter(hasUnread));
	const scroll = createCommentScrollTarget({
		focused: () => focusedComment, comments: () => listedComments, text: () => editText, listOpen: () => commentUI.state?.kind === 'list',
	});
	let docRoot = $state<HTMLElement>();
	let editorSurface = $state<ReturnType<typeof DocViewerEditorSurface>>();
	/** The resolved drawer over the document at every width: its count and whether it is open, shared by the header's control and the surface. */
	let resolvedCount = $state(0), resolvedOpen = $state(false);
	let commentSurface = $state<ReturnType<typeof DocCommentSurface>>();

	// Whether the margin column is up is the surface's own answer, the one its cards and column follow.
	const commentUI = createCommentSurfaceState(() => focusDocumentEditor(docRoot), () => targetKey, () => commentSurface?.cardMode() === 'margin');
	const reviewBarState = createReviewBarState();
	// Below the margin width a card opened from the text is a popover over it: the caret leaving or a key typed in the text closes it, so it never covers what is being written.
	const closeTextCard = () => { if (commentSurface?.cardMode() === 'popover' && commentUI.state?.kind === 'thread' && commentUI.state.fromText) commentUI.close(); };
	const shownFocus = $derived(commentUI.hovered ?? focusedComment);
	openColumnOnLoad({ artifactId: () => loadedArtifactId, ready: () => docState === 'ready' && !!loadedArtifactId && $commentLoads[loadedArtifactId]?.state === 'ready', root: () => docRoot, paneDrawer: isPaneDrawer, hasComments: () => listedComments.length > 0, shown: () => !!commentUI.state, open: commentUI.openOnLoad });
	const heldUnread = createHeldUnread(() => !!commentUI.state, () => listedComments);
	const highlights = $derived(canvasCommentHighlights(listedComments, editText, shownFocus, focusMarker, suggestionBusyId, (comment) => images.groups.colorOf(comment)).map(heldUnread.paint));
	// The phone draft sheet covers the editor's lower part: keep the draft's text where the band above the sheet shows it, on open and once a movement covers it.
	const revealDraft = (opening: boolean) => { const { draft } = draftController; if (draft && (editorSurface?.draftCovered(draft.from, opening) ?? opening)) scroll.value = { from: draft.from, to: draft.to, origin: 'comment-save' }; };
	const openCards = createCommentOpeners(commentUI, scroll);
	// A marker over comments on parts of one image opens the image, at the comment when it is the only one (DCOL-FR13.5).
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null, click?: MouseEvent) { arrivedDocComments.set(new Set(ids)); images.openOr(ids, ids.length === 1 ? { id: ids[0] } : undefined, () => openCards.markers(ids, opener, reading, click)); }
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
		show: (thread, landing) => { if (thread && landing) scroll.value = landing; if (thread) focusedDocComment.set(thread.id); commentUI.state = { kind: 'list' }; if (thread) arrivedDocComments.set(new Set([thread.id])); },
		openImageOr: (id, otherwise) => { images.openOr([id], { id }, otherwise); arrivedDocComments.set(new Set([id])); }, onLanded: () => onLanded?.(),
	});
	export function jumpToUnread(id: string, beforeText?: () => void) { replyNavigation.goTo(id, beforeText); }
	// A card for a comment on part of an image opens the image at its tag; any other card goes to its text.
	function focusFromCard(id: string, placement?: 'top') { images.openOr([id], { id }, () => focusInText(id, placement)); }

	const draftController = createViewerCommentDraft({
		scope: () => targetKey, draft: () => $commentDraft, session: () => liveSession,
		generation: () => liveGeneration, text: () => editText, isOpen: () => commentUI.state?.kind === 'draft',
		ensureArtifactId: ensureEditorArtifactId, onFailure: commentRetryToast.remember,
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
		commentRetryToast.dismissAfterSuccess();
	}
	const threadActions = createThreadActions(() => loadedArtifactId, () => feedbackStatus?.commentCreated(), () => target?.threadId ?? null);

	// Keyed off the load-relevant fields only (not object identity) so an
	// unrelated re-render that recreates the `target` prop object (e.g. a
	// sibling tab's status update) never re-triggers a reload.
	const targetKey = $derived(commentDraftScope(target));
	// A comment the user has typed but not posted: the review bar never answers over it.
	const composerHasText = $derived(Boolean($commentDraft && $commentDraft.scope === targetKey && $commentDraft.body?.trim()));

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
				freshness = value.freshness; ownership = value.ownership ?? null; docState = 'ready'; legacyAwaitingSave = awaiting;
			},
		});
	}
	$effect(() => {
		if (!legacyAwaitingSave || !save.draft || save.draft.status !== 'saved') return;
		legacyAwaitingSave = false;
		reloadNonce += 1;
	});
	reopenOnReconnect(() => $connectionState === 'connected', () => openRefusal?.code === 'not_connected', () => { reloadNonce += 1; });
	function onPrepareCleanReload(): Promise<(() => void) | null> {
		const d = target;
		if (!d || liveSession) return Promise.resolve(null);
		const seq = loadSeq;
		return prepareDocumentReload(d, () => seq === loadSeq, (content) => applyViewerContent(d, content, ++loadSeq));
	}
	$effect(() => {
		const key = targetKey;
		reloadNonce;
		if (!key) return;
		untrack(() => {
			const d = target;
			if (!d) return;
			const seq = ++loadSeq;
			// Every signal carries the tab instance that started this load, so a
			// result that lands after the user switched tabs updates the tab that
			// asked for it rather than whichever tab is active when it settles.
			const signal = (s: Omit<TabLoadSignal, 'instanceId'>) =>
				onSignal?.({ ...s, instanceId: d.instanceId });

			prevTarget = leavePreviousTarget(prevTarget, d);

			draftController.select(null);
			commentUI.closeAll();
			focusedDocComment.set(null); discardCommentDraft(commentDraftScope(prevTarget));
			// These fields describe the mounted target, not its predecessor; a reload of the same file keeps the server's known answer.
			const nextId = withDraftArtifactId(d).artifactId; if (nextId !== loadedArtifactId) serverReadOnly = false; loadedArtifactId = nextId;
			const opened = openFromDraft(existingDraft(d), loadedArtifactId);
			readonly = opened.unsaved;
			confirming = opened.confirming;
			if (opened.text !== null) editText = opened.text;
			freshness = null; ownership = null;
			openRefusal = null; liveUnavailable = null;
			docState = opened.text === null ? 'loading' : 'ready';
			signal(opened.signal);
			void loadViewerContent({
				target: d, isCurrent: () => seq === loadSeq,
				onReady: (content) => { liveUnavailable = content.liveUnavailable; applyViewerContent(d, content, seq); },
				onSnapshot: (text) => { editText = text; },
				onUnavailable: () => signal({ status: 'unavailable' }),
			})
				.catch((e) => {
					if (seq !== loadSeq) return;
					handleViewerFailure(e, opened, { signal, reloadAttention: () => { void reloadAttentionQueue(); },
						fail: (failure) => { openRefusal = failure.refusal; docState = failure.state;
							if (failure.text !== null) editText = failure.text; readonly = failure.readonly; confirming = false; },
					});
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
	<DocViewerStatus
		title={target?.name ?? 'Document'}
		commentsOpen={commentUI.state?.kind === 'list'}
		commentsLoading={docState === 'loading' || (loadedArtifactId ? $commentLoads[loadedArtifactId]?.state === 'loading' : false)}
		saveStatus={history.restoring ? 'saving' : save.status}
		bind:this={feedbackStatus}
		ready={docState === 'ready'}
		stale={stale && !liveSession}
		readonly={readonly && !confirming && !openRefusal}
		commentCount={commentStatus.commentCount}
		unreadCount={unreadThreads.length}
		{resolvedCount} {resolvedOpen} onOpenResolved={() => resolvedOpen = !resolvedOpen}
		artifactId={loadedArtifactId}
		viewingThreadId={target?.threadId ?? null}
		reviewTargetKey={targetKey}
		targetInstanceId={target?.instanceId ?? null}
		imageOpen={covered} activityRoot={docRoot}
		{onReload}
		onOpenComments={(opener) => { history.close(); commentUI.openList(opener); }}
		historyAvailable={history.available}
		historyOpen={history.open}
		onOpenHistory={history.toggle}
		{freshness} {ownership} onOwnershipChanged={(value) => ownership = value}
		onFeedbackChanged={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onMarkVerified={() => verifyAndApplyDocumentFreshness(loadedArtifactId, freshness, (next) => freshness = next)}
		liveSession={liveSession ?? null}
		{openRefusal} {liveUnavailable}
		tabText={editText}
		onReopen={() => reloadNonce += 1}
		onRead={() => reloadNonce += 1}
	>
		{#snippet reviewBar()}{#if !commentUI.sheetUp}{@render reviewControls(false)}{/if}{/snippet}
		{#snippet presence()}
			{#if target && loadedArtifactId}
				<DocPresenceStatus {target} artifactId={loadedArtifactId} {docState} root={docRoot}
					textCurrent={liveConnection === null || liveConnection === 'connected' || liveConnection === 'refused'}
					projectComment={(comment) => commentsInDocumentOrder([comment], editText, $suggestionRefusals[loadedArtifactId!], suggestionBusyId)[0]}
					bind:ranges={presenceRanges} {onPrepareCleanReload}
					onArrival={(frame) => void activity?.arrive(frame)}
					onFeedback={(id, feedback) => feedbackStatus?.sync(id, feedback)} />
			{/if}
		{/snippet}
	</DocViewerStatus>
	{#if target}<DocBottomBand artifactId={loadedArtifactId} threads={unreadThreads} root={docRoot} ready={docState === 'ready'} {covered} sheetModalOpen={commentUI.sheetUp} onJump={(thread) => { commentUI.restoreReplyFocus(() => docRoot); replyNavigation.goTo(thread.id); }} />{/if}
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
		onAnchorFocus={openCards.comment}
		onCaretAway={() => { if (!commentUI.release()) closeTextCard(); }}
		onTyping={closeTextCard}
		cardMode={commentSurface?.cardMode()}
		onSuggestionHover={commentUI.hover}
		onEdit={onDocEdit}
		onLayout={(mapPos) => activity?.editorChanged(mapPos)}
		readOnly={readonly || confirming || suggestionBusyId !== null}
		imageBase={target && { ...target, imageCacheKey: imageKey.current }}
		onOpenImage={onOpenImage ? (request) => images.open(request) : undefined}
		tabInstanceId={target?.instanceId ?? null}
		{onDocumentPlace}
		saveError={save.error}
		pendingSel={draftController.pending}
		commentSheetOpen={commentUI.sheetUp}
		onStartComment={draftController.start}
	/>
	{#if history.open && history.available && target}<DocHistorySurface {target} readOnly={serverReadOnly} applyRestore={history.restore} onRestored={async () => { (await onPrepareCleanReload())?.(); }} bind:restoring={() => history.restoring, history.setRestoring} narrow={isPaneDrawer()} within={docRoot?.closest<HTMLElement>('.leaf-body') ?? docRoot} {covered} onClose={history.close} />{/if}
	<DocCommentSurface
		bind:this={commentSurface}
		reviewBar={reviewControls}
		reviewVisible={commentUI.sheetUp}
		state={commentUI.state}
		anchor={commentUI.anchor}
		anchorAtClick={commentUI.atClick}
		bounds={docRoot}
		narrow={isPaneDrawer()}
		phone={$isMobile}
		onRelease={commentUI.release}
		quiet={commentUI.quiet}
		comments={listedComments}
		widgetLabel={{ read: (from, to, quote) => editorSurface?.widgetLabel(from, to, quote), stamp: () => editorSurface?.widgetLabelStamp() }}
		focusedId={shownFocus}
		draft={draftController.draft}
		draftReason={draftController.reason}
		onDraftBody={(body) => setCommentBody(body, targetKey)}
		onCancelDraft={draftController.cancel}
		onClose={commentUI.close}
		onThreadMissing={commentUI.focusRemainingThread}
		restoreFocus={commentUI.restoreFocus}
		onFocus={focusFromCard}
		load={loadedArtifactId ? $commentLoads[loadedArtifactId] : undefined}
		onRetry={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onSubmit={submitComment}
		onReply={threadActions.reply}
		onViewed={(id, replyId) => loadedArtifactId ? markThreadViewed(loadedArtifactId, id, replyId) : Promise.resolve()}
		onResolve={threadActions.resolve}
		onAgree={threadActions.agree}
		{target}
		artifactId={loadedArtifactId}
		readOnly={readonly || confirming}
		onSuggestionBusy={(busy, id) => suggestionBusyId = busy ? id ?? '' : null}
		onSuggestionAccepted={(text) => { if (!liveSession) editText = text; }}
		onDocumentChanged={reload}
		onSheetModalChange={(open) => commentUI.sheetUp = open}
		onRevealDraft={revealDraft}
		images={images.groups}
		{covered}
		{readStampSuppressed}
		draftOverColumn={commentUI.draftOverColumn}
		bind:resolvedCount bind:resolvedOpen
	/>
</div>
{#snippet reviewControls(inSheet: boolean)}
	<ReviewBar artifactId={loadedArtifactId} bar={reviewBarState} draftOpen={composerHasText} suppressed={commentUI.state !== null && commentUI.state !== undefined && !inSheet} {inSheet} pendingSuggestions={readonly || confirming ? 0 : openSuggestionCount(listedComments)} onAcceptAll={async () => { suggestionBusyId = ''; try { await acceptAllOpen(target, loadedArtifactId ?? '', { onText: (text) => { if (!liveSession) editText = text; }, onDocumentChanged: reload }); } finally { suggestionBusyId = null; } }} onAnswered={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)} />
{/snippet}
<!-- Styles are in DocViewer.css. -->
