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
	import { createDocViewerImages } from '$lib/docViewerImages';
	import type { DocumentImageRequest } from '$lib/stores/documentImageViewer';
	import { projects } from '$lib/stores/projects';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import { docOpenRefusal, ImageFileError, type DocOpenRefusal } from '$lib/docOpenRefusal';
	import type { DocumentFreshness } from '$lib/docFreshness';
	import { createCommentRetryToast, createThreadActions, markFreshnessVerified, prepareDocumentReload, reloadDocumentFeedback, teardownDocumentViewer } from '$lib/docViewerOperations';
	import { handleDocumentKey } from '$lib/docViewerKeyboard';
	import { createCommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';
	import { railWidth } from '$lib/cm/comment-clusters';
	import { canvasCommentHighlights, commentSurfaceStatus, canvasCommentScrollTarget, canvasFocusedCommentScrollTarget, commentsInDocumentOrder, type CanvasScrollTarget } from '$lib/docCanvasComments';
	import { loadViewerContent, viewerContent, type ViewerContent } from '$lib/docViewerLoading';
	import { liveDocumentBindings } from '$lib/stores/liveDocuments';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import { syncActiveDocumentEditor } from '$lib/docViewerEditorState';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { TabLoadSignal } from '$lib/artifactTabs';
	import {
		docSaveDrafts, draftKey, effectiveDraftTarget, existingDraft, seedDraft,
		enrichDraftTarget, setDraftText, flushDraft,
	} from '$lib/stores/docSaveQueue';
	import { docSaveGhost } from '$lib/stores/docSaveGhost.svelte';
	import DocCommentSurface from './DocCommentSurface.svelte';
	import DocViewerEditorSurface from './DocViewerEditorSurface.svelte';
	import DocPresenceStatus from './DocPresenceStatus.svelte';
	import DocCanvasActivity from './DocCanvasActivity.svelte';
	import DocViewerStatus from './DocViewerStatus.svelte';
	import type { FeedbackSummary } from './DocFeedbackStatus.svelte';
	import ReviewBar from './ReviewBar.svelte';
	import { toasts } from '$lib/stores/toast';
	import { shouldReloadPresenceEdit } from '$lib/docPresence';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import type { ScrollOrigin } from '$lib/cm/comment-marks';
	import DocumentViewport from './DocumentViewport.svelte';

	type DocState = 'loading' | 'ready' | 'error';

	let {
		target,
		onClose = () => {},
		escapeCloses = true,
		onSignal,
		stale = false,
		onReload,
		onOpenImage,
		covered = false,
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
		/** The canvas shows an image viewer over this document; it keeps its state but answers no keys. */
		covered?: boolean;
	} = $props();

	let docState = $state<DocState>('loading');
	let loadError = $state('');
	let openRefusal = $state<DocOpenRefusal | null>(null);
	let liveUnavailable = $state<ViewerContent['liveUnavailable']>(null);
	let loadedArtifactId = $state<string | null>(null);
	let readonly = $state(false);
	let freshness = $state<DocumentFreshness | null>(null);
	const focusedComment = $derived($focusedDocComment);

	let editText = $state('');
	let suggestionBusyId = $state<string | null>(null);
	let feedbackStatus = $state<DocViewerStatus>();
	let feedbackSummary = $state<FeedbackSummary>({ pending: 0, owner: null, graceMinutes: null, sending: false });

	// Reactive save state for the currently loaded target, sourced from the
	// per-tab-instance save queue — never a single shared "current doc" flag.
	const currentDraftKey = $derived(target ? draftKey(target) : null);
	const currentDraft = $derived(currentDraftKey ? $docSaveDrafts[currentDraftKey] : undefined);
	const saveError = $derived(currentDraft?.status === 'error' ? currentDraft.error : '');
	const liveSession = $derived(target ? $liveDocumentBindings[target.instanceId] : undefined);
	let liveConnection = $state<LiveDocumentSession['connection'] | null>(null);
	let liveGeneration = $state(0);
	let legacyAwaitingSave = $state(false);
	$effect(() => {
		const session = liveSession;
		if (!session) { liveConnection = null; return; }
		return session.subscribe(state => {
			editText = state.text.toString();
			readonly = !state.writable;
			liveConnection = state.connection;
			liveGeneration = state.textGeneration;
		});
	});

	const saveGhost = docSaveGhost(() => currentDraft?.status);
	const saveStatus = $derived(saveGhost.current);
	const commentRetryToast = createCommentRetryToast(toasts);

	let presenceRanges = $state<PresenceRange[]>([]);
	let activity = $state<DocCanvasActivity>();

	// The comments pane derives orphaning from the text currently on screen (and a refused accept)
	// so a comment never remains highlighted after an in-editor edit changes it.
	$effect(() => syncActiveDocumentEditor({ target, draft: currentDraft, docState, artifactId: loadedArtifactId, text: editText, readonly }));

	const listedComments = $derived(commentsInDocumentOrder(loadedArtifactId ? ($docComments[loadedArtifactId] ?? []) : [], editText, loadedArtifactId ? $suggestionRefusals[loadedArtifactId] : undefined));
	const { orphanedCount, placementReloading, placementReloadError } = $derived(commentSurfaceStatus(listedComments));
	let scrollTarget = $state<CanvasScrollTarget | null>(null);
	$effect(() => {
		const fc = focusedComment;
		if (!fc) { scrollTarget = null; return; }
		untrack(() => {
			scrollTarget = canvasFocusedCommentScrollTarget(scrollTarget, listedComments, editText, fc, commentUI.state?.kind === 'list');
		});
	});
	let docRoot = $state<HTMLElement>();
	const commentUI = createCommentSurfaceState(() => docRoot?.querySelector<HTMLElement>('.cm-content')?.focus(), () => targetKey);
	const shownFocus = $derived(commentUI.hovered ?? focusedComment);
	const highlights = $derived(canvasCommentHighlights(listedComments, editText, shownFocus, focusMarker, suggestionBusyId));
	function setCommentScrollTarget(id: string, focus: boolean, origin: ScrollOrigin) {
		scrollTarget = canvasCommentScrollTarget(listedComments, editText, id, focus, origin);
	}
	function focusComment(id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean) {
		if (!commentUI.focusComment(id, anchor, viaSuggestion)) return;
		setCommentScrollTarget(id, false, 'navigation');
	}
	function showMarkerCards(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null) { if (ids.length === 1) setCommentScrollTarget(ids[0], false, 'navigation'); commentUI.focusMarker(ids, opener, reading); }
	// A marker over comments on parts of one image opens the image, at the comment when it is the only one (DCOL-FR13.5).
	function focusMarker(ids: string[], opener: HTMLElement, reading?: () => DOMRect | null) { images.openOr(ids, ids.length === 1 ? { id: ids[0] } : undefined, () => showMarkerCards(ids, opener, reading)); }
	function onDocEdit(mapPos: (p: number) => number) { if (loadedArtifactId) remapComments(loadedArtifactId, mapPos); images.remap(mapPos); }

	const accent = $derived($projects.find((project) => project.id === target?.projectId)?.accent_color ?? null);
	const images = createDocViewerImages({
		onOpenImage: () => onOpenImage, root: () => docRoot, comments: () => listedComments, artifactId: () => loadedArtifactId,
		target: () => target, readOnly: () => readonly, text: () => editText, session: () => liveSession, accent: () => accent,
		feedback: () => feedbackStatus?.feedback(), showInText: focusInText });
	function focusInText(id: string) { setCommentScrollTarget(id, commentUI.state?.kind === 'list', 'navigation'); focusedDocComment.set(id); }
	// A card for a comment on part of an image opens the image at its tag; any other card goes to its text.
	function focusFromCard(id: string) { images.openOr([id], { id }, () => focusInText(id)); }

	const draftController = createViewerCommentDraft({
		scope: () => targetKey, draft: () => $commentDraft, session: () => liveSession,
		generation: () => liveGeneration, text: () => editText, isOpen: () => commentUI.state?.kind === 'draft',
		ensureArtifactId: ensureEditorArtifactId, onFailure: commentRetryToast.remember,
		onStart: commentUI.openDraft, onClose: commentUI.close,
	});
	async function submitComment(text: string) {
		const id = await draftController.submit(text);
		if (!id) return;
		setCommentScrollTarget(id, true, 'comment-save');
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
	// Feedback loads share the comment store; each call only decides whether this tab may apply its result.
	// The freshness state remains here; the verifier performs only the API transition.
	function applyViewerContent(d: DocViewerTarget, content: ViewerContent, seq: number): void {
		if (seq !== loadSeq) return;
		seedDraft(d, content.text, content.revision);
		if (content.artifactId && !d.artifactId) enrichDraftTarget(d, content.artifactId);
		editText = content.text;
		loadedArtifactId = content.artifactId;
		readonly = !content.writable;
		freshness = content.freshness;
		docState = 'ready';
		legacyAwaitingSave = !content.live && Boolean(existingDraft(d)?.editText !== existingDraft(d)?.savedText);
		onSignal?.({ status: 'ready', artifactId: content.artifactId, revision: content.revision, writable: content.writable, instanceId: d.instanceId });
		reloadDocumentFeedback(content.artifactId, seq, () => loadSeq, feedbackStatus);
	}
	$effect(() => {
		if (!legacyAwaitingSave || !currentDraft || currentDraft.status !== 'saved') return;
		legacyAwaitingSave = false;
		reloadNonce += 1;
	});
	function onPrepareCleanReload(): Promise<(() => void) | null> {
		const d = target;
		if (!d || liveSession) return Promise.resolve(null);
		const seq = loadSeq;
		return prepareDocumentReload(
			d,
			() => seq === loadSeq && shouldReloadPresenceEdit(existingDraft(d)),
			effectiveDraftTarget(d).artifactId,
			(response, fallbackArtifactId) => applyViewerContent(d, viewerContent(response, fallbackArtifactId), ++loadSeq),
		);
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
			const existing = existingDraft(d);
			const dirty = Boolean(existing && existing.editText !== existing.savedText);
			// These fields describe the mounted target, not its predecessor. Unsaved
			// local text wins over server content, but not over write authority: it
			// stays visible and read-only until the loader re-checks write access.
			loadedArtifactId = effectiveDraftTarget(d).artifactId;
			readonly = dirty;
			if (existing) editText = existing.editText;
			freshness = null;
			loadError = '';
			openRefusal = null; liveUnavailable = null;
			docState = dirty ? 'ready' : 'loading';
			signal(dirty ? { status: 'ready', artifactId: loadedArtifactId, writable: false } : { status: 'loading' });
			void loadViewerContent({
				target: d, isCurrent: () => seq === loadSeq,
				onReady: (content) => { liveUnavailable = content.liveUnavailable; applyViewerContent(d, content, seq); },
				onSnapshot: (text) => { editText = text; },
				onUnavailable: () => signal({ status: 'unavailable' }),
			})
				.catch((e) => {
					if (seq !== loadSeq) return;
					// A tab opened before the backend's type was known: the leaf swaps it to the image viewer.
					if (e instanceof ImageFileError) { signal({ status: 'loading', renderer: 'image' }); return; }
					openRefusal = docOpenRefusal(e);
					loadError = e instanceof Error ? e.message : String(e);
					docState = openRefusal || dirty ? 'ready' : 'error';
					if (existing) editText = existing.editText;
					readonly = Boolean(openRefusal);
					// A 404 means the document is gone, which the tab shows as its
					// explicit unavailable state rather than a raw load error.
					signal({ status: /(^|\s)404(\s|$)/.test(loadError) ? 'unavailable' : 'error' });
				});
		});
	});

	$effect(() => {
		const text = editText;
		if (docState !== 'ready' || !target || readonly || openRefusal) return;
		setDraftText(target, text);
	});

	function flushNow() {
		if (target && !readonly) void flushDraft(target);
	}

	onDestroy(() => teardownDocumentViewer(target, flushNow, releaseDocEditor));

	async function markCurrentDocumentVerified(): Promise<void> {
		freshness = await markFreshnessVerified(loadedArtifactId, freshness);
	}

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
		bind:this={feedbackStatus}
		bind:feedbackSummary
		ready={docState === 'ready'}
		stale={stale && !liveSession}
		readonly={readonly && !openRefusal}
		commentCount={listedComments.filter((comment) => !comment.resolved).length}
		{orphanedCount}
		artifactId={loadedArtifactId}
		viewingThreadId={target?.threadId ?? null}
		reviewTargetKey={targetKey}
		targetInstanceId={target?.instanceId ?? null}
		{onReload}
		onReloadPlacements={loadedArtifactId ? () => reloadPlacements(loadedArtifactId!) : undefined}
		{placementReloading}
		{placementReloadError}
		{liveConnection}
		onOpenComments={commentUI.openList}
		{freshness}
		onFeedbackChanged={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)}
		onMarkVerified={markCurrentDocumentVerified}
		liveSession={liveSession ?? null}
		{openRefusal} {liveUnavailable}
		tabText={editText}
		onReopen={() => reloadNonce += 1}
		onRead={() => reloadNonce += 1}
	>
		{#snippet reviewBar()}<ReviewBar artifactId={loadedArtifactId} draftOpen={composerHasText} suppressed={commentUI.state !== null && commentUI.state !== undefined} onAnswered={() => reloadDocumentFeedback(loadedArtifactId, undefined, () => loadSeq, feedbackStatus)} />{/snippet}
		{#snippet presence()}
			{#if target && loadedArtifactId}
				<DocPresenceStatus {target} artifactId={loadedArtifactId} {docState} root={docRoot}
					bind:ranges={presenceRanges} {onPrepareCleanReload}
					onArrival={(frame) => void activity?.arrive(frame)}
					onFeedback={(id, feedback) => feedbackStatus?.sync(id, feedback)} />
			{/if}
		{/snippet}
	</DocViewerStatus>
	<DocCanvasActivity bind:this={activity} root={docRoot} artifactId={loadedArtifactId} threadId={target?.threadId ?? null} onJump={(span) => scrollTarget = { ...span, origin: 'navigation' }} />
	<DocViewerEditorSurface
		{docState}
		{loadError}
		bind:value={editText}
		liveText={liveSession?.text ?? null} typingAllowed={liveConnection === 'connected'} previews={liveSession ?? null}
		{highlights}
		presence={presenceRanges}
		pendingComment={draftController.pendingComment}
		scrollTo={scrollTarget}
		onSave={flushNow}
		onSelect={draftController.select}
		onAnchorFocus={focusComment}
		onSuggestionHover={commentUI.hover}
		onEdit={onDocEdit}
		onLayout={(mapPos) => activity?.editorChanged(mapPos)}
		readOnly={readonly || suggestionBusyId !== null}
		imageBase={target}
		onOpenImage={onOpenImage ? (request) => images.open(request) : undefined}
		{saveError}
		{saveStatus}
		pendingSel={draftController.pending}
		onStartComment={draftController.start}
	/>
	<DocCommentSurface
		state={commentUI.state}
		anchor={commentUI.anchor}
		bounds={docRoot}
		narrow={isPaneDrawer()}
		comments={listedComments}
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
		onSubmit={submitComment}
		onReply={threadActions.reply}
		onViewed={(id, replyId) => loadedArtifactId ? markThreadViewed(loadedArtifactId, id, replyId) : Promise.resolve()}
		onResolve={threadActions.resolve}
		onAgree={threadActions.agree}
		{target}
		artifactId={loadedArtifactId}
		readOnly={readonly}
		onSuggestionBusy={(busy, id) => suggestionBusyId = busy ? id ?? '' : null}
		onSuggestionAccepted={(text) => { if (!liveSession) editText = text; }}
		onDocumentChanged={reload}
		images={images.groups}
	/>
</div>

<!-- Styles are in DocViewer.css. -->
