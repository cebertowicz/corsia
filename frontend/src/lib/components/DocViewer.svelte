<script lang="ts">
	import { fade } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import './DocViewer.css';
	import { docCanvasBounds } from '$lib/actions/docCanvasBounds';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { onDestroy, untrack } from 'svelte';
	import { ensureEditorArtifactId, releaseDocEditor } from '$lib/stores/activeDocEditor';
	import { docComments, commentLoads, remapComments, markThreadViewed, reloadPlacements } from '$lib/stores/docComments';
	import { suggestionRefusals } from '$lib/stores/suggestionRefusals';
	import { focusedDocComment } from '$lib/stores/docCommentFocus';
	import { commentDraft, commentDraftScope, discardCommentDraft, setCommentBody } from '$lib/stores/commentDraft';
	import { createViewerCommentDraft } from '$lib/docViewerCommentDraft.svelte';
	import { createCommentScrollTarget } from '$lib/docViewerScrollTarget.svelte';
	import { createReplyLanding } from '$lib/docReplyLanding.svelte';
	import { createDocViewerImages, type DocumentImageRequest } from '$lib/docViewerImages';
	import { projects } from '$lib/stores/projects';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import type { DocOpenRefusal } from '$lib/docOpenRefusal';
	import { handleViewerFailure } from '$lib/docViewerFailure';
	import { applyDocumentContent } from '$lib/docViewerApplyContent';
	import { bindViewerSession, reopenOnReconnect } from '$lib/docViewerLiveBinding.svelte';
	import { bindViewerDraft } from '$lib/docViewerDraftBinding.svelte';
	import type { DocumentFreshness } from '$lib/docFreshness';
	import { createCommentRetryToast, createThreadActions, prepareDocumentReload, reloadDocumentFeedback, teardownDocumentViewer, verifyAndApplyDocumentFreshness } from '$lib/docViewerOperations';
	import { handleDocumentKey } from '$lib/docViewerKeyboard';
	import { acceptAllOpen, openSuggestionCount } from '$lib/docAcceptAll';
	import { createCommentSurfaceState, focusDocumentEditor } from '$lib/docCommentSurfaceState.svelte';
	import { railWidth } from '$lib/cm/comment-clusters';
	import { awaitsFirstMark, canvasCommentHighlights, canvasCommentScrollTarget, commentSurfaceStatus, commentsInDocumentOrder } from '$lib/docCanvasComments';
	import { loadViewerContent, openFromDraft, type ViewerContent } from '$lib/docViewerLoading';
	import { liveDocumentBindings } from '$lib/stores/liveDocuments';
	import { connectionState } from '$lib/stores/connection';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import { syncActiveDocumentEditor } from '$lib/docViewerEditorState';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import {
		withDraftArtifactId, existingDraft,
		flushDraft,
	} from '$lib/stores/docSaveQueue';
	import { createDocSaveState } from '$lib/docViewerSaveState.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';
	import { reload as reloadAttentionQueue } from '$lib/stores/attentionDock.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import DocViewerEditorSurface from './DocViewerEditorSurface.svelte';
	import DocPresenceStatus from './DocPresenceStatus.svelte';
	import DocCanvasActivity from './DocCanvasActivity.svelte';
	import DocViewerStatus from './DocViewerStatus.svelte';
	import ReviewBar from './ReviewBar.svelte';
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
		onLanded,
	}: {
		target: DocViewerTarget | null;
		onClose?: () => void;
		escapeCloses?: boolean;
		/** Reports load progress against the tab instance that started it. */
		onSignal?: (signal: TabLoadSignal) => void;
		/** A newer revision exists than the one on screen. */
		stale?: boolean;
		onReload?: () => void;
		/** Opens an image of the document larger in the canvas, with its comments. */
		onOpenImage?: (request: DocumentImageRequest) => void;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
		/** The canvas shows an image viewer over this document; it keeps its state but answers no keys. */
		covered?: boolean;
		/** The tab's reply landing was acted on, or had no readable replies; the tab drops it. */
		onLanded?: () => void;
	} = $props();

	let docState = $state<DocState>('loading');
	let loadError = $state('');
	let openRefusal = $state<DocOpenRefusal | null>(null);
	let liveUnavailable = $state<ViewerContent['liveUnavailable']>(null);
	let loadedArtifactId = $state<string | null>(null);
	let readonly = $state(false);
	let confirming = $state(false);
	let freshness = $state<DocumentFreshness | null>(null);
	const focusedComment = $derived($focusedDocComment);

	let editText = $state('');
	let suggestionBusyId = $state<string | null>(null);
	let feedbackStatus = $state<DocViewerStatus>();

	const save = createDocSaveState(() => target);
	const liveSession = $derived(target ? $liveDocumentBindings[target.instanceId] : undefined);
	let liveConnection = $state<LiveDocumentSession['connection'] | null>(null);
	let liveGeneration = $state(0);
	let legacyAwaitingSave = $state(false);
	bindViewerSession(() => liveSession, (state) => {
		if (!state) { liveConnection = null; return; }
		editText = state.text.toString();
		readonly = !state.writable;
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

	const listedComments = $derived(commentsInDocumentOrder(loadedArtifactId ? ($docComments[loadedArtifactId] ?? []) : [], editText, loadedArtifactId ? $suggestionRefusals[loadedArtifactId] : undefined));
	const commentStatus = $derived(commentSurfaceStatus(listedComments));
	const unreadThreads = $derived(listedComments.filter((comment) => (comment.newReplyIds?.length ?? 0) > 0));
	const firstUnreadThread = $derived(unreadThreads[0] ?? null);
	const firstUnreadTarget = $derived(firstUnreadThread ? canvasCommentScrollTarget(listedComments, editText, firstUnreadThread.id, false, 'quote-tap') : null);
	const scroll = createCommentScrollTarget({
		focused: () => focusedComment, comments: () => listedComments, text: () => editText, listOpen: () => commentUI.state?.kind === 'list',
	});
	let docRoot = $state<HTMLElement>();
	let editorSurface = $state<ReturnType<typeof DocViewerEditorSurface>>();

	const commentUI = createCommentSurfaceState(() => focusDocumentEditor(docRoot), () => targetKey);
	const shownFocus = $derived(commentUI.hovered ?? focusedComment);
	const highlights = $derived(canvasCommentHighlights(listedComments, editText, shownFocus, focusMarker, suggestionBusyId));
	// The phone draft sheet covers the editor's lower part: keep the draft's text where the band above the sheet shows it, on open and once a movement covers it.
	const revealDraft = (opening: boolean) => { const { draft } = draftController; if (draft && (editorSurface?.draftCovered(draft.from, opening) ?? opening)) scroll.value = { from: draft.from, to: draft.to, origin: 'comment-save' }; };
	function focusComment(id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean) {
		if (!commentUI.focusComment(id, anchor, viaSuggestion)) return;
		scroll.toComment(id, false, 'navigation');
	}
	function showMarkerCards(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null) { if (ids.length === 1) scroll.toComment(ids[0], false, 'navigation'); commentUI.focusMarker(ids, opener, reading); }
	// A marker over comments on parts of one image opens the image, at the comment when it is the only one (DCOL-FR13.5).
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null) { images.openOr(ids, ids.length === 1 ? { id: ids[0] } : undefined, () => showMarkerCards(ids, opener, reading)); }
	function onDocEdit(mapPos: (p: number) => number) { if (loadedArtifactId) remapComments(loadedArtifactId, mapPos); images.remap(mapPos); }

	const accent = $derived($projects.find((project) => project.id === target?.projectId)?.accent_color ?? null);
	const images = createDocViewerImages({
		onOpenImage: () => onOpenImage, root: () => docRoot, comments: () => listedComments, artifactId: () => loadedArtifactId,
		target: () => target, readOnly: () => readonly || confirming, text: () => editText, session: () => liveSession, accent: () => accent,
		feedback: () => feedbackStatus?.feedback(), imageCacheKey: () => imageKey.current, showInText: focusInText });
	/** The comments of the image shown over this document, bound to it as it is now: an image's host rebuilds them when it shows one. */
	export function imageComments() { return images.comments(); }
	function focusInText(id: string, placement?: 'top') { scroll.toComment(id, commentUI.state?.kind === 'list', placement === 'top' ? 'quote-tap' : 'navigation'); focusedDocComment.set(id); }
	createReplyLanding({
		target: () => target,
		ready: () => !!loadedArtifactId && $commentLoads[loadedArtifactId]?.state === 'ready' && docState === 'ready',
		first: () => firstUnreadThread, landing: () => firstUnreadTarget,
		open: (first, landing) => {
			if (first && landing) { scroll.value = landing; focusedDocComment.set(first.id); }
			commentUI.state = { kind: 'list' };
			onLanded?.();
		},
	});
	// A card for a comment on part of an image opens the image at its tag; any other card goes to its text.
	function focusFromCard(id: string, placement?: 'top') { images.openOr([id], { id }, () => focusInText(id, placement)); }

	const draftController = createViewerCommentDraft({
		scope: () => targetKey, draft: () => $commentDraft, session: () => liveSession,
		generation: () => liveGeneration, text: () => editText, isOpen: () => commentUI.state?.kind === 'draft',
		ensureArtifactId: ensureEditorArtifactId, onFailure: commentRetryToast.remember,
		onStart: commentUI.openDraft, onClose: commentUI.close,
		awaitsMark: (id) => awaitsFirstMark(listedComments.find((item) => item.id === id), highlights.some((range) => range.id === id)),
	});
	async function submitComment(text: string) {
		const id = await draftController.submit(text);
		if (!id) return;
		scroll.toComment(id, true, 'comment-save');
		focusedDocComment.set(id);
		commentUI.state = { kind: 'thread', id };
		commentDraft.set(null);
		feedbackStatus?.commentCreated();
		commentRetryToast.dismissAfterSuccess();
	}
	const threadActions = createThreadActions(() => loadedArtifactId, () => feedbackStatus?.commentCreated());

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
				editText = value.text; loadedArtifactId = value.artifactId; readonly = !value.writable; confirming = false;
				freshness = value.freshness; docState = 'ready'; legacyAwaitingSave = awaiting;
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

			// Switching stays immediate: flush the previous target's draft in
			// the background rather than awaiting it here.
			if (prevTarget && prevTarget.instanceId !== d.instanceId) {
				void flushDraft(prevTarget);
				discardCommentDraft(commentDraftScope(prevTarget));
			}
			prevTarget = d;

			draftController.select(null);
			commentUI.close();
			focusedDocComment.set(null); discardCommentDraft(commentDraftScope(prevTarget));
			// These fields describe the mounted target, not its predecessor.
			loadedArtifactId = withDraftArtifactId(d).artifactId;
			const opened = openFromDraft(existingDraft(d), loadedArtifactId);
			readonly = opened.unsaved;
			confirming = opened.confirming;
			if (opened.text !== null) editText = opened.text;
			freshness = null;
			loadError = '';
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
						fail: (failure) => { openRefusal = failure.refusal; loadError = failure.message; docState = failure.state;
							if (failure.text !== null) editText = failure.text; readonly = failure.readonly; confirming = false; },
					});
				});
		});
	});

	const flushNow = bindViewerDraft({ target: () => target, text: () => editText,
		canEdit: () => docState === 'ready' && !readonly && !confirming && !openRefusal,
		canFlush: () => !readonly && !confirming });

	onDestroy(() => teardownDocumentViewer(target, flushNow, releaseDocEditor));

	function onKey(event: KeyboardEvent): void {
		handleDocumentKey(event, {
			covered, commentUI, draftController,
			paneOpen: $paneOpen, isPaneDrawer: isPaneDrawer(),
			escapeCloses, flush: flushNow, close: onClose,
		});
	}
</script>

<svelte:window onkeydown={onKey} />
<DocumentViewport instanceId={target?.instanceId} ready={docState === 'ready'} root={docRoot} />

<div class="doc" style:--doc-rail="{railWidth()}px" use:docCanvasBounds bind:this={docRoot} in:fade={{ duration: $prefersReducedMotion ? 0 : 160, easing: cubicOut }}>
	<DocViewerStatus
		title={target?.name ?? 'Document'}
		commentsOpen={commentUI.state?.kind === 'list'}
		commentsLoading={docState === 'loading' || (loadedArtifactId ? $commentLoads[loadedArtifactId]?.state === 'loading' : false)}
		saveStatus={save.status}
		bind:this={feedbackStatus}
		ready={docState === 'ready'}
		stale={stale && !liveSession}
		readonly={readonly && !confirming && !openRefusal}
		commentCount={commentStatus.commentCount}
		unreadCount={unreadThreads.length}
		orphanedCount={commentStatus.orphanedCount}
		artifactId={loadedArtifactId}
		viewingThreadId={target?.threadId ?? null}
		reviewTargetKey={targetKey}
		targetInstanceId={target?.instanceId ?? null}
		imageOpen={covered} activityRoot={docRoot}
		{onReload}
		onOpenComments={commentUI.openList}
		{freshness}
		onFeedbackChanged={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onMarkVerified={() => verifyAndApplyDocumentFreshness(loadedArtifactId, freshness, (next) => freshness = next)}
		liveSession={liveSession ?? null}
		{openRefusal} {liveUnavailable}
		tabText={editText}
		onReopen={() => reloadNonce += 1}
		onRead={() => reloadNonce += 1}
	>
		{#snippet reviewBar()}<ReviewBar artifactId={loadedArtifactId} draftOpen={composerHasText} suppressed={commentUI.state !== null && commentUI.state !== undefined} pendingSuggestions={readonly || confirming ? 0 : openSuggestionCount(listedComments)} onAcceptAll={async () => { suggestionBusyId = ''; try { await acceptAllOpen(target, loadedArtifactId ?? '', { onText: (text) => { if (!liveSession) editText = text; }, onDocumentChanged: reload }); } finally { suggestionBusyId = null; } }} onAnswered={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)} />{/snippet}
		{#snippet presence()}
			{#if target && loadedArtifactId}
				<DocPresenceStatus {target} artifactId={loadedArtifactId} {docState} root={docRoot}
					bind:ranges={presenceRanges} {onPrepareCleanReload}
					onArrival={(frame) => void activity?.arrive(frame)}
					onFeedback={(id, feedback) => feedbackStatus?.sync(id, feedback)} />
			{/if}
		{/snippet}
	</DocViewerStatus>
	<DocCanvasActivity bind:this={activity} root={docRoot} artifactId={loadedArtifactId} threadId={target?.threadId ?? null} onJump={(span) => scroll.value = { ...span, origin: 'navigation' }} />
	<DocViewerEditorSurface
		bind:this={editorSurface}
		{docState}
		{loadError}
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
		onAnchorFocus={focusComment}
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
		commentSheetOpen={commentUI.sheetModalOpen}
		onStartComment={draftController.start}
	/>
	<DocCommentSurface
		state={commentUI.state}
		anchor={commentUI.anchor}
		bounds={docRoot}
		narrow={isPaneDrawer()}
		comments={listedComments}
		widgetLabel={{ read: (from, to, quote) => editorSurface?.widgetLabel(from, to, quote), stamp: () => editorSurface?.widgetLabelStamp() }}
		focusedId={shownFocus}
		draft={draftController.draft}
		draftReason={draftController.reason}
		onDraftBody={(body) => setCommentBody(body, targetKey)}
		onCancelDraft={draftController.cancel}
		onClose={commentUI.close}
		restoreFocus={commentUI.restoreFocus}
		onFocus={focusFromCard}
		load={loadedArtifactId ? $commentLoads[loadedArtifactId] : undefined}
		onRetry={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		placementRecovery={{ onReload: loadedArtifactId && liveSession ? () => reloadPlacements(loadedArtifactId) : undefined, unplacedCount: commentStatus.orphanedCount, replaceableCount: commentStatus.replaceableCount, reloading: commentStatus.placementReloading, reloadError: commentStatus.placementReloadError, unavailable: liveConnection === 'disconnected' || liveConnection === 'reconnecting', connectionDescriptionId: liveConnectionDescriptionId(liveSession?.artifactId) }}
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
		onSheetModalChange={(open) => commentUI.sheetModalOpen = open}
		onRevealDraft={revealDraft}
		images={images.groups}
		{covered}
	/>
</div>
<!-- Styles are in DocViewer.css. -->
