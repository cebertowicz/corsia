<script lang="ts">
	import type * as Y from 'yjs';
	import type { CommentRange, ScrollOrigin } from '$lib/cm/comment-marks';
	import type { CommentSelection } from '$lib/stores/commentSelection';
	import type { PresenceRange } from '$lib/cm/presence-marks';
	import type { PreviewFrameSource } from '$lib/stores/livePreviewFeed';
	import type { OpenImageRequest } from '$lib/cm/markdown-image';
	import { openDocumentLink } from '$lib/linkTargetResolver';
	import MarkdownEditor from './MarkdownEditor.svelte';

	type DocState = 'loading' | 'ready' | 'error';
	type PendingSelection = { from: number; to: number; text: string; top: number; left: number; anchor?: () => DOMRect | null };

	let {
		docState,
		loadError,
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
		saveStatus,
		pendingSel,
		onStartComment,
		tabInstanceId = null,
		onDocumentPlace,
	}: {
		docState: DocState;
		loadError: string;
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
		imageBase?: { path: string; projectId: string | null } | null;
		onOpenImage?: (request: OpenImageRequest) => void;
		saveError: string;
		saveStatus: 'idle' | 'saving' | 'saved';
		pendingSel: PendingSelection | null;
		onStartComment: () => void;
		tabInstanceId?: string | null;
		onDocumentPlace?: (instanceId: string, place: { from: number; top: number; scrollTop: number }) => void;
	} = $props();

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
		<div class="load-error">
			<p>Couldn't open this document.</p>
			<code>{loadError}</code>
		</div>
	{:else}
		<div class="ed-wrap">
			<MarkdownEditor bind:value liveText={liveText} {typingAllowed} {previews} {highlights} {presence} {pendingComment} {scrollTo} {onSave} {onSelect} {onAnchorFocus} {onSuggestionHover} {onEdit} {onLayout} {readOnly} {imageBase} {onOpenImage} {tabInstanceId} {onDocumentPlace} onOpenLink={imageBase ? (href) => void openDocumentLink(href, imageBase.path) : undefined} />
			{#if saveError}
				<div class="save-error" role="alert">{saveError}</div>
			{/if}
		</div>
	{/if}
</div>

{#if saveStatus !== 'idle'}
	<div class="save-ghost" class:saving={saveStatus === 'saving'}>{saveStatus === 'saving' ? 'Saving…' : '✓ Saved'}</div>
{/if}

{#if pendingSel}
	<button class="sel-comment" style="top:{pendingSel.top}px; left:{buttonLeft}px" bind:offsetWidth={buttonWidth}
		onmousedown={(event) => event.preventDefault()} onclick={onStartComment}>
		<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
		Comment
	</button>
{/if}
