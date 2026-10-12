<script lang="ts">
	// The open document's feedback batch, owned here for as long as the document is open: leaving it (another
	// document, a hidden browser tab, closing) lets its comments go to the owner after the grace period.
	import { onMount } from 'svelte';
	import type { CommentFeedback } from '$lib/api/docs';
	import { holdDocFeedback, type DocFeedback } from '$lib/docFeedback.svelte';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';
	import DocFeedbackControls from './DocFeedbackControls.svelte';

	let {
		artifactId,
		targetInstanceId,
		onFeedbackChanged,
		compact = false,
	}: {
		artifactId: string | null;
		targetInstanceId: string | null;
		onFeedbackChanged?: () => void;
		/** Shown in a narrow bar (DocFeedbackControls). */
		compact?: boolean;
	} = $props();

	const feedback = holdDocFeedback(() => ({ artifactId, targetInstanceId }), () => onFeedbackChanged?.());

	export function commentCreated() { feedback.commentCreated(); }
	export function sync(id: string, current: CommentFeedback) { feedback.sync(id, current); }
	/** The batch itself, for an image viewer over this document to show and send. */
	export function controller(): DocFeedback { return feedback; }

	onMount(() => subscribeToDocumentCommentChanges(
		() => feedback.artifactId,
		() => onFeedbackChanged?.(),
	));
</script>

<DocFeedbackControls {feedback} {compact} />
