<script lang="ts">
	// The open document's feedback state, owned here for as long as the document is open. The server delivers a
	// comment as it is made and an edit once typing pauses; a re-opened comment waits here until it goes.
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
		withSend = true,
	}: {
		artifactId: string | null;
		targetInstanceId: string | null;
		onFeedbackChanged?: () => void;
		/** Shown in a narrow bar (DocFeedbackControls). */
		compact?: boolean;
		/** Show Send here (DocFeedbackControls); false where another surface shows it. */
		withSend?: boolean;
	} = $props();

	const feedback = holdDocFeedback(() => ({ artifactId, targetInstanceId }), () => onFeedbackChanged?.());

	export function commentCreated() { feedback.commentCreated(); }
	export function sync(id: string, current: CommentFeedback) { feedback.sync(id, current); }
	/** The batch itself, for the header's unsent mark, the comments drawer's Send now and an image viewer over this document. */
	export function controller(): DocFeedback { return feedback; }

	onMount(() => subscribeToDocumentCommentChanges(
		() => feedback.artifactId,
		() => onFeedbackChanged?.(),
	));
</script>

<DocFeedbackControls {feedback} {compact} {withSend} />
