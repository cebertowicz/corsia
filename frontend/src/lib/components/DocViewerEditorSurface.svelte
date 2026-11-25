<script lang="ts">
	import { mount, unmount, untrack } from 'svelte';
	import type * as Y from 'yjs';
	import type { ChangeDesc } from '@codemirror/state';
	import type { SuggestionHistoryEntry } from '$lib/cm/suggestionHistory';
	import type { CommentRange, ScrollOrigin } from '$lib/cm/comment-marks';
	import type { CommentSelection } from '$lib/stores/commentSelection';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import type { PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import type { OpenImageRequest } from '$lib/cm/markdown-image';
	import { openDocumentLink } from '$lib/linkTargetResolver';
	import { menuPop } from '$lib/popoverMotion';
	import { coarsePointer, keepInBand, selectionButtonPlace } from '$lib/selectionButtonPlace';
	import { visibleRegion } from '$lib/cm/visible-region';
	import type { SelectionOffer } from '$lib/cm/widget-selection-types';
	import type { CardMode } from '$lib/docCommentSurfaceState.svelte';
	import DocLoadState from './DocLoadState.svelte';
	import MarkdownEditor from './MarkdownEditor.svelte';
	import DocTouchCommentBar from './DocTouchCommentBar.svelte';
	import EmailConversation from './EmailConversation.svelte';
	import EmailNotes from './EmailNotes.svelte';
	import EmailAttachments from './EmailAttachments.svelte';
	import EmailSendBar from './EmailSendBar.svelte';
	import { currentRecipients, headerLayout, newerEmails, newerEmailsLine, notFullAddressLine, refusedEntries, repliedToId, type EmailDraftContext } from '$lib/emailDraft';
	import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';
	import { fetchEmailDraftContext, readEmailDraftState } from '$lib/api/emailDrafts';
	import { useEmailHeader, type EmailHeaderOptions } from '$lib/cm/email-header-slot.svelte';
	import { activeThreadId } from '$lib/stores/activeThreadId';
	import DocStaleChoice from './DocStaleChoice.svelte';
	import { docSaveDrafts } from '$lib/stores/docSaveQueue';
	import { EDITOR_STALE_STATUS } from '$lib/stores/docSaveBase';
	import { showsSaveBar } from '$lib/stores/docSaveBars';
	import type { ViewerContent } from '$lib/docViewerLoading';

	type DocState = 'loading' | 'ready' | 'error';
	type PendingSelection = SelectionOffer;

	let {
		docState,
		documentName = 'this document',
		onRetry = () => {},
		value = $bindable(''),
		liveText = null,
		typingAllowed = false,
		previews = null,
		highlights,
		presence,
		pendingComment = null,
		scrollTo,
		onSave,
		onFlush = async () => true,
		onSelect,
		onAnchorFocus,
		onCaretAway,
		onSuggestionHover,
		onEdit,
		onLayout,
		readOnly,
		reading = false,
		touchReading = false,
		imageBase = null,
		onOpenImage,
		saveError,
		pendingSel,
		listOverText = false,
		cardMode,
		onTyping,
		onStartComment,
		tabInstanceId = null,
		onDocumentPlace,
		artifactId = null,
		onSuggestionHistory,
		onSuggestionHistoryRecovery,
		onUseFile,
	}: {
		docState: DocState;
		documentName?: string;
		onRetry?: () => void;
		value?: string;
		liveText?: Y.Text | null;
		typingAllowed?: boolean;
		previews?: PreviewFrameSource | null;
		highlights: CommentRange[];
		presence: PresenceRange[];
		pendingComment?: CommentSelection | null;
		scrollTo: { from: number; to: number; focus?: boolean; origin: ScrollOrigin } | null;
		onSave: () => void;
		/** Saves every pending edit and resolves once the server holds them; false when the save failed. */
		onFlush?: () => Promise<boolean>;
		onSelect: (selection: PendingSelection | null) => void;
		onAnchorFocus: (id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean, click?: MouseEvent) => void;
		onCaretAway?: () => void;
		onSuggestionHover?: (id: string | null) => void;
		onEdit: (changes: ChangeDesc, open: ReadonlySet<string>) => void;
		onLayout: (mapPos?: (position: number) => number) => void;
		readOnly: boolean;
		reading?: boolean;
		touchReading?: boolean;
		imageBase?: { path: string; projectId: string | null; imageCacheKey?: string } | null;
		onOpenImage?: (request: OpenImageRequest) => void;
		saveError: string;
		pendingSel: PendingSelection | null;
		/** The comment list is over the text (the sheet, whose closing scrim stays modal until its outro ends, or the list popover). */
		listOverText?: boolean;
		/** How a card opened from the text shows now (DocCommentSurface): over the text, as a popover or the sheet, only a click opens one. */
		cardMode?: CardMode;
		onTyping?: () => void;
		onStartComment: () => void;
		tabInstanceId?: string | null;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
		/** The loaded document's artifact id. When its draft record makes it an email draft, it opens as an email with its context read from the draft's account. */
		artifactId?: string | null;
		onSuggestionHistory?: (entry: SuggestionHistoryEntry, before: string, after: string) => Promise<boolean>;
		onSuggestionHistoryRecovery?: () => void;
		/** The viewer's one content writer after the first load (also a clean reload's): the file's text the person chose after a stale refusal goes into the editor through it. */
		onUseFile: (content: ViewerContent) => void;
	} = $props();

	let editor = $state<MarkdownEditor>();
	let sendEnded = $state(false);
	// An email draft keeps its text in the editor, with the warnings above it and the read-only conversation below. The
	// editor sits in the same wrapper either way, so editing the header never remounts it. `readAttempt` counts the
	// person's retries of a failed read; each one reads the draft's context again.
	let readAttempt = $state(0);
	let context = $state<{ artifactId: string; read: EmailDraftContext } | null>(null);
	const LOADING: EmailDraftContext = { status: 'loading', conversation: [], notes: [], attachments: [] };
	const draft = $derived(artifactId ? $emailDraftHeaders[artifactId] ?? null : null);
	const emailDraftId = $derived(draft ? artifactId : null);
	$effect(() => {
		const id = docState === 'ready' ? emailDraftId : null;
		readAttempt;
		if (!id) { context = null; return; }
		let current = true;
		context = { artifactId: id, read: untrack(() => context)?.artifactId === id ? { ...untrack(() => context)!.read, status: 'loading' } : LOADING };
		void fetchEmailDraftContext(id).then((read) => { if (current) context = { artifactId: id, read }; });
		return () => { current = false; };
	});
	const email = $derived<EmailDraftContext | null>(context?.read ?? null);
	const attachments = $derived(email?.attachments ?? []);
	// Which entries the send would refuse is the server's reading of the header (refusedEntries says how an edit it
	// hasn't read is shown).
	const rereads = $derived(!!liveText);
	let refused = $state<string[]>([]);
	$effect(() => {
		const recipients = currentRecipients(draft?.header, headerLayout(value.split('\n')).text);
		refused = draft ? refusedEntries(untrack(() => refused), recipients, rereads) : [];
	});
	const loudLines = $derived(email ? [newerEmailsLine(newerEmails(email)), notFullAddressLine(refused)].filter((line): line is string => !!line) : []);
	const emailHeader = $derived<EmailHeaderOptions | null>(email ? {
		quiet: email.notes,
		answered: email.answered ?? null,
		header: draft?.header ?? null,
		rereads,
		attachmentsKey: JSON.stringify([attachments, email.threadId ?? $activeThreadId]),
		mountAttachments: attachments.length ? (host) => {
			const chips = mount(EmailAttachments, { target: host, props: { attachments, threadId: email.threadId ?? $activeThreadId } });
			return () => void unmount(chips);
		} : null,
	} : null);
	// The header lines are drawn as a compact block over the editable text (cm/email-header.ts).
	useEmailHeader(() => editor?.editorView(), () => emailHeader);
	/** The label a comment on a table's text gets, read from the editor's own syntax tree; undefined while the editor is not up. */
	export function widgetLabel(from: number | undefined, to: number | undefined, quote: string) { return editor?.widgetLabel(from, to, quote); }
	export function widgetLabelStamp() { return editor?.widgetLabelStamp(); }
	export function coveredBySheet(from: number, opening: boolean) { return editor?.coveredBySheet(from, opening); }
	export function editorView() { return editor?.editorView(); }
	export function acceptSuggestion(text: string, eventId: string, ids: string[]) { return editor?.acceptSuggestion(text, eventId, ids) ?? false; }
	export function clearInputSelection() { editor?.clearInputSelection(); }

	// The button follows the selection's end but never leaves the visible document column, and sits above or below the rows the
	// selection is drawn on, as chat's does, so it never covers selected text.
	const coarse = coarsePointer();
	// The button is fixed to the window, so it is read again whenever the document scrolls: a selection that is taller than the
	// window, or has scrolled away, keeps it at the edge of what the reader sees nearest the selection.
	let scrolled = $state(0);
	$effect(() => {
		const view = pendingSel ? untrack(() => editor?.editorView()) : undefined;
		if (!view) return;
		const scroller = visibleRegion(view).scroller;
		const bump = () => { scrolled += 1; };
		scroller.addEventListener('scroll', bump, { passive: true });
		return () => scroller.removeEventListener('scroll', bump);
	});
	let buttonHeight = $state(0);
	const place = $derived.by(() => {
		if (!pendingSel) return null;
		void scrolled;
		// An offer placed by its widget (above a cell, or at the point it gives) sits as given.
		if (pendingSel.above || pendingSel.exact) return { top: pendingSel.top, above: !!pendingSel.above, below: !!pendingSel.below };
		const placed = selectionButtonPlace(pendingSel.anchor?.() ?? { top: pendingSel.top, bottom: pendingSel.top }, coarse);
		const view = editor?.editorView();
		const top = view ? keepInBand(placed, visibleRegion(view).band, buttonHeight, EDGE) : placed.top;
		return { top, above: placed.above, below: !placed.above };
	});
	const refusedStale = $derived(!!tabInstanceId && !!$docSaveDrafts[tabInstanceId]?.stale);
	const EDGE = 8;
	let column: HTMLDivElement | undefined = $state();
	let buttonWidth = $state(0);
	const buttonLeft = $derived.by(() => {
		if (!pendingSel) return 0;
		const wanted = (pendingSel.exact ? pendingSel.left : pendingSel.left + 8) - (pendingSel.end ? buttonWidth : 0);
		const bounds = column?.getBoundingClientRect();
		if (!bounds) return wanted;
		return Math.max(bounds.left + EDGE, Math.min(wanted, bounds.right - EDGE - buttonWidth));
	});
</script>

{#snippet editorBlock()}
	<div class="ed-wrap" use:showsSaveBar={tabInstanceId ?? ''}>
		<MarkdownEditor bind:this={editor} bind:value liveText={liveText} {typingAllowed} {previews} {highlights} {presence} {pendingComment} {scrollTo} {onSave} {onSelect} {onAnchorFocus} {onCaretAway} {cardMode} {onTyping} {onSuggestionHover} {onEdit} {onLayout} readOnly={readOnly || sendEnded} {reading} {imageBase} {onOpenImage} {tabInstanceId} {onDocumentPlace} {onSuggestionHistory} {onSuggestionHistoryRecovery} onOpenLink={imageBase ? (href) => void openDocumentLink(href, imageBase.path) : undefined} />
		<!-- A refusal stays on screen while a choice's write runs (the draft is 'saving' then, with no error), so Details is not closed and rebuilt. -->
		{#if saveError || refusedStale}
			<div class="save-error" role="alert">{saveError || EDITOR_STALE_STATUS}{#if tabInstanceId && refusedStale}<DocStaleChoice tabKey={tabInstanceId} {onUseFile} />{/if}</div>
		{/if}
	</div>
{/snippet}

<div class="doc-main" class:email-mode={email} class:touch-reading={touchReading} bind:this={column}>
	{#if docState === 'ready' && draft && artifactId}
		{#key artifactId}
			<EmailSendBar {artifactId} text={() => editor?.editorView()?.state.doc.toString() ?? value} targetVersion={email?.targetVersion ?? null} readAt={email?.readAt ?? null} initialState={draft.state ?? 'open'} {readOnly} connected={typingAllowed} blocker={attachments.find((item) => item.unavailable)?.name ?? null} bind:ended={sendEnded} flush={onFlush} readState={() => readEmailDraftState(artifactId!)} onReread={() => readAttempt += 1} onRefused={(reason) => { context = { artifactId: artifactId!, read: { status: 'error', reason, conversation: [], notes: [], attachments: [] } }; }} />
		{/key}
	{/if}
	{#if docState === 'loading' || docState === 'error'}
		<DocLoadState state={docState} name={documentName} {onRetry} />
	{:else}
		<div class="doc-body" class:email-scroll={email}>
			{#if email}<EmailNotes lines={loudLines} />{/if}
			{@render editorBlock()}
			{#if email && (email.conversation.length || email.status !== 'ready')}
				<EmailConversation messages={email.conversation} repliedTo={repliedToId(email)} status={email.status} reason={email.reason} reference={email.reference} detail={email.detail} onretry={() => readAttempt += 1} />
			{/if}
		</div>
	{/if}
</div>

{#snippet commentFace()}
	<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
	Comment
{/snippet}

{#if touchReading}
	<DocTouchCommentBar {column} offered={!!pendingSel && !listOverText}>
		<button class="sel-comment" onpointerdown={(event) => event.preventDefault()} onmousedown={(event) => event.preventDefault()} onclick={() => { if (!listOverText) onStartComment(); }}>{@render commentFace()}</button>
	</DocTouchCommentBar>
{:else if pendingSel && !listOverText}
	<button class="sel-comment" class:above={place?.above} class:below={place?.below} out:menuPop style="top:{place?.top}px; left:{buttonLeft}px" bind:offsetWidth={buttonWidth} bind:offsetHeight={buttonHeight}
		onmousedown={(event) => event.preventDefault()} onclick={() => { if (!listOverText) onStartComment(); }}>
		{@render commentFace()}
	</button>
{/if}

<style>
	/* The editor's own end room (40vh, which lets the last line scroll above a keyboard) stays; the Comment button's room only raises it when it would be smaller. A scroll into view leaves exactly what the button covers clear: a margin card's field lying just above the bar must not count as hidden, or the browser re-centres the document on it. */
	.touch-reading :global(.cm-content:not(.cm-md-cell-editor .cm-content)) { padding-bottom: max(40vh, calc(var(--document-comment-room, 60px) + var(--space-4))); }
	.touch-reading :global(.cm-scroller:not(.cm-md-cell-editor .cm-scroller)) { scroll-padding-bottom: var(--document-comment-room, 60px); }
</style>
