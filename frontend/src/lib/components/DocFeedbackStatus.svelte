<script lang="ts">
	// The open document's feedback state, owned here for as long as the document is open. The server delivers a
	// comment as it is made and an edit once typing pauses; a re-opened comment waits here until it goes.
	import { onMount } from 'svelte';
	import type { CommentFeedback } from '$lib/api/docs';
	import { type DocFeedback, type HeldLane } from '$lib/docFeedback.svelte';
	import { holdDocFeedback } from '$lib/docFeedbackLifecycle.svelte';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';
	import DocFeedbackControls from './DocFeedbackControls.svelte';
	import { THREAD_SENT_EVENT } from '$lib/heldDocFeedbackScope';

	let {
		artifactId,
		targetInstanceId,
		onFeedbackChanged,
		compact = false,
		lane = null,
	}: {
		artifactId: string | null;
		targetInstanceId: string | null;
		onFeedbackChanged?: () => void | Promise<void>;
		/** Shown in a narrow bar (DocFeedbackControls). */
		compact?: boolean;
		/** The held lane whose Send shows here (DocFeedbackControls); none where another surface shows it. */
		lane?: HeldLane | null;
	} = $props();

	const feedback = holdDocFeedback(() => ({ artifactId, targetInstanceId }), () => onFeedbackChanged?.());

	export function commentCreated() {
		feedback.commentCreated();
		onFeedbackChanged?.();
	}
	export function sync(id: string, current: CommentFeedback) { feedback.sync(id, current); }
	/** The batch itself, for the header's unsent mark and an image viewer over this document. */
	export function controller(): DocFeedback { return feedback; }

	onMount(() => {
		const unsubscribe = subscribeToDocumentCommentChanges(
			() => feedback.artifactId,
			() => onFeedbackChanged?.(),
		);
		// The chat's Send closed document-held comments somewhere: read what this document still holds.
		const threadSent = () => onFeedbackChanged?.();
		window.addEventListener(THREAD_SENT_EVENT, threadSent);
		return () => {
			unsubscribe();
			window.removeEventListener(THREAD_SENT_EVENT, threadSent);
		};
	});
</script>

<DocFeedbackControls {feedback} {compact} {lane} />
