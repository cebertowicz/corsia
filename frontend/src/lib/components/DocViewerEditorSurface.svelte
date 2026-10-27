<script lang="ts">
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

	type DocState = 'loading' | 'ready' | 'error';
	type PendingSelection = { from: number; to: number; text: string; top: number; left: number; anchor?: () => DOMRect | null; above?: boolean; label?: SelectionLabel; inWidget?: boolean };

	let {
		docState,
		loadError,
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
		onSuggestionHover,
		onEdit,
		onLayout,
		readOnly,
		imageBase = null,
		onOpenImage,
		saveError,
		pendingSel,
		commentSheetOpen = false,
		onStartComment,
		tabInstanceId = null,
		onDocumentPlace,
	}: {
		docState: DocState;
		loadError: string;
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
		onAnchorFocus: (id: string, anchor: () => DOMRect | null, viaSuggestion?: boolean) => void;
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
		onStartComment: () => void;
		tabInstanceId?: string | null;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
	} = $props();

	let editor = $state<MarkdownEditor>();
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

<div class="doc-main" bind:this={column}>
	{#if docState === 'loading'}
		<div class="loadbar"><span></span></div>
		<div class="md-skel">{#each Array(6) as _, i (i)}<span class="ms-line" style="width:{[90, 70, 80, 55, 75, 45][i]}%"></span>{/each}</div>
	{:else if docState === 'error'}
		<div class="load-error" role="alert">
			<p>Couldn't open {documentName}.</p>
			<Button size="sm" variant="ghost" onclick={onRetry}>Try again</Button>
		</div>
	{:else}
		<div class="ed-wrap">
			<MarkdownEditor bind:this={editor} bind:value liveText={liveText} {typingAllowed} {previews} {highlights} {presence} {pendingComment} {scrollTo} {onSave} {onSelect} {onAnchorFocus} {onSuggestionHover} {onEdit} {onLayout} {readOnly} {imageBase} {onOpenImage} {tabInstanceId} {onDocumentPlace} onOpenLink={imageBase ? (href) => void openDocumentLink(href, imageBase.path) : undefined} />
			{#if saveError}
				<div class="save-error" role="alert">{saveError}</div>
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
