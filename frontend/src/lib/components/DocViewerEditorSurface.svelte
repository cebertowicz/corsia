<script lang="ts">
	import { mount, unmount, untrack } from 'svelte';
	import type * as Y from 'yjs';
	import type { ChangeDesc } from '@codemirror/state';
	import type { CommentRange, ScrollOrigin } from '$lib/cm/comment-marks';
	import type { CommentSelection } from '$lib/stores/commentSelection';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import type { PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import type { OpenImageRequest } from '$lib/cm/markdown-image';
	import { openDocumentLink } from '$lib/linkTargetResolver';
	import { menuPop } from '$lib/popoverMotion';
	import type { SelectionLabel } from '$lib/stores/commentDraft';
	import type { CardMode } from '$lib/docCommentSurfaceState.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import MarkdownEditor from './MarkdownEditor.svelte';
	import EmailConversation from './EmailConversation.svelte';
	import EmailNotes from './EmailNotes.svelte';
	import EmailAttachments from './EmailAttachments.svelte';
	import { badEntries, newerEmails, newerEmailsLine, notFullAddressLine, repliedToId, type EmailDraftContext } from '$lib/emailDraft';
	import { fetchEmailDraftContext } from '$lib/api/emailDrafts';
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
		cardMode,
		onTyping,
		onStartComment,
		tabInstanceId = null,
		artifactId = null,
		onDocumentPlace,
		emailDraft = false,
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
		onEdit: (changes: ChangeDesc, open: ReadonlySet<string>) => void;
		onLayout: (mapPos?: (position: number) => number) => void;
		readOnly: boolean;
		imageBase?: { path: string; projectId: string | null; imageCacheKey?: string } | null;
		onOpenImage?: (request: OpenImageRequest) => void;
		saveError: string;
		pendingSel: PendingSelection | null;
		/** Routed from createCommentSheet; its closing scrim remains modal until the outro ends. */
		commentSheetOpen?: boolean;
		/** How a card opened from the text shows now (DocCommentSurface): over the text, as a popover or the sheet, only a click opens one. */
		cardMode?: CardMode;
		onTyping?: () => void;
		onStartComment: () => void;
		tabInstanceId?: string | null;
		artifactId?: string | null;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
		/** The backend's presentation says this document is an email draft: it opens as an email, its context read from the draft's account. */
		emailDraft?: boolean;
	} = $props();

	let editor = $state<MarkdownEditor>();
	// An email draft keeps its text in the editor, with the warnings above it and the read-only conversation below. The
	// editor sits in the same wrapper either way, so editing the header never remounts it. `readAttempt` counts the
	// person's retries of a failed read; each one reads the draft's context again.
	let readAttempt = $state(0);
	let context = $state<{ artifactId: string; read: EmailDraftContext } | null>(null);
	const LOADING: EmailDraftContext = { status: 'loading', conversation: [], notes: [], attachments: [] };
	$effect(() => {
		const id = emailDraft && docState === 'ready' ? artifactId : null;
		readAttempt;
		if (!id) { context = null; return; }
		let current = true;
		context = { artifactId: id, read: untrack(() => context)?.artifactId === id ? { ...untrack(() => context)!.read, status: 'loading' } : LOADING };
		void fetchEmailDraftContext(id).then((read) => { if (current) context = { artifactId: id, read }; });
		return () => { current = false; };
	});
	const email = $derived<EmailDraftContext | null>(context?.read ?? null);
	const attachments = $derived(email?.attachments ?? []);
	// Who the email goes to is read from the draft's own text, so the warnings follow each edit.
	const refused = $derived(email ? badEntries(value) : []);
	const loudLines = $derived(email ? [newerEmailsLine(newerEmails(email)), notFullAddressLine(refused)].filter((line): line is string => !!line) : []);
	const emailHeader = $derived<EmailHeaderOptions | null>(email ? {
		quiet: email.notes,
		answered: email.answered ?? null,
		attachmentsKey: JSON.stringify(attachments),
		mountAttachments: attachments.length ? (host) => {
			const chips = mount(EmailAttachments, { target: host, props: { attachments, threadId: $activeThreadId } });
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
		<MarkdownEditor bind:this={editor} bind:value liveText={liveText} {typingAllowed} {previews} {highlights} {presence} {pendingComment} {scrollTo} {onSave} {onSelect} {onAnchorFocus} {onCaretAway} {cardMode} {onTyping} {onSuggestionHover} {onEdit} {onLayout} {readOnly} {imageBase} {onOpenImage} {tabInstanceId} {onDocumentPlace} {emailHeader} onOpenLink={imageBase ? (href) => void openDocumentLink(href, imageBase.path) : undefined} />
		{#if saveError}
			<div class="save-error" role="alert">{saveError}</div>
		{/if}
	</div>
{/snippet}

<div class="doc-main" class:email-mode={email} bind:this={column}>
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
				<EmailConversation messages={email.conversation} repliedTo={repliedToId(email)} status={email.status} unreadable={email.unreadable} onretry={() => readAttempt += 1} />
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
