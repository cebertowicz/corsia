<script lang="ts">
	import { mount, unmount } from 'svelte';
	import type * as Y from 'yjs';
	import type { CommentRange, ScrollOrigin } from '$lib/cm/comment-marks';
	import type { CommentSelection } from '$lib/stores/commentSelection';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import type { PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import type { OpenImageRequest } from '$lib/cm/markdown-image';
	import { openDocumentLink } from '$lib/linkTargetResolver';
	import { menuPop } from '$lib/popoverMotion';
	import type { SelectionLabel } from '$lib/stores/commentDraft';
	import Button from '$lib/components/ui/Button.svelte';
	import MarkdownEditor from './MarkdownEditor.svelte';
	import EmailConversation from './EmailConversation.svelte';
	import EmailNotes from './EmailNotes.svelte';
	import EmailAttachments from './EmailAttachments.svelte';
	import EmailSendBar from './EmailSendBar.svelte';
	import { addressesOnAnsweredEmail, badEntriesIn, emailDraftContext, newerEmails, newerEmailsLine, notFullAddressLine, repliedToId, type EmailAttachment, type EmailDraftContext } from '$lib/emailDraft';
	import type { EmailHeaderOptions } from '$lib/cm/email-header';
	import { activeThreadId } from '$lib/stores/activeThreadId';

	type DocState = 'loading' | 'ready' | 'error';
	type PendingSelection = { from: number; to: number; text: string; top: number; left: number; anchor?: () => DOMRect | null; above?: boolean; label?: SelectionLabel; inWidget?: boolean };

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
		onSelect,
		onAnchorFocus,
		onCaretAway,
		onSuggestionHover,
		onEdit,
		onLayout,
		readOnly,
		imageBase = null,
		onOpenImage,
		saveError,
		pendingSel,
		commentSheetOpen = false,
		commentsAsSheet = false,
		onStartComment,
		tabInstanceId = null,
		artifactId = null,
		onDocumentPlace,
		reviewReleased = $bindable(false),
		reviewAnswersShown = false,
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
		onSelect: (selection: PendingSelection | null) => void;
		onAnchorFocus: (id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean, click?: MouseEvent) => void;
		onCaretAway?: () => void;
		onSuggestionHover?: (id: string | null) => void;
		onEdit: (mapPos: (position: number) => number) => void;
		onLayout: (mapPos?: (position: number) => number) => void;
		readOnly: boolean;
		imageBase?: { path: string; projectId: string | null; imageCacheKey?: string } | null;
		onOpenImage?: (request: OpenImageRequest) => void;
		saveError: string;
		pendingSel: PendingSelection | null;
		/** Routed from createCommentSheet; its closing scrim remains modal until the outro ends. */
		commentSheetOpen?: boolean;
		/** Cards open as the phone sheet (the pane-drawer width): a caret in a suggestion is editing and opens none. */
		commentsAsSheet?: boolean;
		onStartComment: () => void;
		tabInstanceId?: string | null;
		artifactId?: string | null;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
		/** Out: the email draft's send is over and its send bar holds no answer (none on its way, none to retry); the review bar answers a send request then. */
		reviewReleased?: boolean;
		/** In: the canvas's review bar is showing the draft's open request with its answers (the send bar's unanswered sentence points at it only then). */
		reviewAnswersShown?: boolean;
	} = $props();

	let sendEnded = $state(false);
	let editor = $state<MarkdownEditor>();
	// An email draft keeps its text in the editor, with the safeguard's warnings above it and the read-only conversation
	// below. The editor sits in the same wrapper either way, so editing the header never remounts it.
	// `readAttempt` counts the person's retries of a failed conversation read.
	let readAttempt = $state(0);
	const email = $derived<EmailDraftContext | null>(docState === 'ready' ? emailDraftContext(value, undefined, readAttempt) : null);
	let conversation = $state<EmailConversation>();
	// The attachments are read from the draft record; removing one re-saves the draft through the existing save path
	// (prototype: only the list changes). The prototype's files live in the folder `corsia:emailDraftFiles` names.
	let removedFiles = $state<Set<string>>(new Set());
	// A sent or not-to-send email, or a draft the tab can't edit, is read-only: no typing, no Cc control, no chip remove marks.
	const emailLocked = $derived(!!email && (readOnly || email.readOnly === true || sendEnded));
	const attachments = $derived<EmailAttachment[]>((email?.attachments ?? []).filter((item) => !removedFiles.has(item.name)).map((item) => ({ ...item, path: prototypeFilePath(item.path) })));
	function prototypeFilePath(name: string): string {
		try { const dir = localStorage.getItem('corsia:emailDraftFiles'); return dir ? `${dir.replace(/\/$/, '')}/${name}` : name; } catch { return name; }
	}
	const badEntries = $derived(badEntriesIn(value, email?.badRecipients));
	const loudLines = $derived(email ? [newerEmailsLine(newerEmails(email)), notFullAddressLine(badEntries), ...email.notes.filter((note) => note.kind === 'loud').map((note) => note.text)].filter((line): line is string => !!line) : []);
	const quietNotes = $derived(email?.notes.filter((note) => note.kind === 'quiet').map((note) => note.text) ?? []);
	const emailHeader = $derived<EmailHeaderOptions | null>(email ? {
		quiet: quietNotes,
		bad: badEntries,
		answered: email ? addressesOnAnsweredEmail(email) : null,
		attachmentsKey: JSON.stringify([attachments, emailLocked]),
		mountAttachments: attachments.length ? (host) => {
			const chips = mount(EmailAttachments, { target: host, props: {
				attachments, threadId: $activeThreadId, openedFrom: tabInstanceId,
				onremove: emailLocked ? undefined : (item: EmailAttachment) => { removedFiles = new Set([...removedFiles, item.name]); },
			} });
			return () => void unmount(chips);
		} : null,
	} : null);
	/** The label a comment on a table's text gets, read from the editor's own syntax tree; undefined while the editor is not up. */
	export function widgetLabel(from: number | undefined, to: number | undefined, quote: string) { return editor?.widgetLabel(from, to, quote); }
	export function widgetLabelStamp() { return editor?.widgetLabelStamp(); }
	export function draftCovered(from: number, opening: boolean) { return editor?.draftCovered(from, opening); }

	// The button follows the selection's end but never leaves the visible document column.
	const EDGE = 8;
	let column: HTMLDivElement | undefined = $state();
	let buttonWidth = $state(0);
	const buttonLeft = $derived.by(() => {
		if (!pendingSel) return 0;
		const wanted = pendingSel.left + 8;
		const bounds = column?.getBoundingClientRect();
		if (!bounds) return wanted;
		return Math.max(bounds.left + EDGE, Math.min(wanted, bounds.right - EDGE - buttonWidth));
	});
</script>

{#snippet editorBlock()}
	<div class="ed-wrap">
		<MarkdownEditor bind:this={editor} bind:value liveText={liveText} {typingAllowed} {previews} {highlights} {presence} {pendingComment} {scrollTo} {onSave} {onSelect} {onAnchorFocus} {commentsAsSheet} {onSuggestionHover} {onEdit} {onLayout} {onCaretAway} readOnly={readOnly || emailLocked} {imageBase} {onOpenImage} {tabInstanceId} {onDocumentPlace} {emailHeader} onOpenLink={imageBase ? (href) => void openDocumentLink(href, imageBase.path) : undefined} />
		{#if saveError}
			<div class="save-error" role="alert">{saveError}</div>
		{/if}
	</div>
{/snippet}

<div class="doc-main" class:email-mode={email} bind:this={column}>
	{#if email && docState === 'ready'}<EmailSendBar readOnly={readOnly || !!email.readOnly} connected={typingAllowed && !email.offline} blocker={attachments.find((item) => item.unavailable)?.name ?? null} sendEnds={email.sendEnds} reviewArtifactId={email.review ? artifactId : null} bind:ended={sendEnded} bind:answersFree={reviewReleased} answersShown={reviewAnswersShown} />{/if}
	{#if docState === 'loading'}
		<div class="loadbar"><span></span></div>
		<div class="md-skel">{#each Array(6) as _, i (i)}<span class="ms-line" style="width:{[90, 70, 80, 55, 75, 45][i]}%"></span>{/each}</div>
	{:else if docState === 'error'}
		<div class="load-error" role="alert">
			<p>Couldn't open {documentName}.</p>
			<Button size="sm" variant="ghost" onclick={onRetry}>Retry</Button>
		</div>
	{:else}
		<div class="doc-body" class:email-scroll={email}>
			{#if email}<EmailNotes lines={loudLines} />{/if}
			{@render editorBlock()}
			{#if email && (email.conversation.length || email.status !== 'ready')}
				<EmailConversation bind:this={conversation} messages={email.conversation} repliedTo={repliedToId(email)} status={email.status} unreadable={email.unreadable} comments={email.comments} onretry={() => readAttempt += 1} />
			{/if}
		</div>
	{/if}
</div>

{#if pendingSel && !commentSheetOpen}
	<button class="sel-comment" class:above={pendingSel.above} out:menuPop style="top:{pendingSel.top}px; left:{buttonLeft}px" bind:offsetWidth={buttonWidth}
		onmousedown={(event) => event.preventDefault()} onclick={() => { if (!commentSheetOpen) onStartComment(); }}>
		<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
		Comment
	</button>
{/if}
