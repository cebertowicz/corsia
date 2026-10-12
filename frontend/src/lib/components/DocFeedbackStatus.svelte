<script lang="ts">
	// The open document's feedback batch, owned here for as long as the document is open: leaving it (another
	// document, a hidden browser tab, closing) lets its comments go to the owner after the grace period.
	import { onMount } from 'svelte';
	import type { CommentFeedback } from '$lib/api/docs';
	import { holdDocFeedback, type DocFeedback } from '$lib/docFeedback.svelte';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';
	import DocFeedbackControls from './DocFeedbackControls.svelte';

	export type FeedbackSummary = { pending: number; owner: string | null; graceMinutes: number | null; sending: boolean };

	let {
		artifactId,
		targetInstanceId,
		onFeedbackChanged,
		compact = false,
		withSend = true,
		summary = $bindable({ pending: 0, owner: null, graceMinutes: null, sending: false }),
	}: {
		artifactId: string | null;
		targetInstanceId: string | null;
		onFeedbackChanged?: () => void;
		/** Shown in a narrow bar (DocFeedbackControls). */
		compact?: boolean;
		/** Show Send here (DocFeedbackControls); false where another surface shows it. */
		withSend?: boolean;
		/** Header and drawer state derived from the one feedback owner. */
		summary?: FeedbackSummary;
	} = $props();

	const feedback = holdDocFeedback(() => ({ artifactId, targetInstanceId }), () => onFeedbackChanged?.());

	$effect(() => {
		summary = {
			pending: feedback.pendingCount,
			owner: feedback.ownerName,
			graceMinutes: feedback.sendingGrace ? feedback.graceMinutes : null,
			sending: feedback.sending,
		};
	});

	export function commentCreated() { feedback.commentCreated(); }
	export function sync(id: string, current: CommentFeedback) { feedback.sync(id, current); }
	/** The batch itself, for an image viewer over this document to show and send. */
	export function controller(): DocFeedback { return feedback; }
	export function sendNow(): Promise<void> { return feedback.send(); }

	onMount(() => subscribeToDocumentCommentChanges(
		() => feedback.artifactId,
		() => onFeedbackChanged?.(),
	));
</script>

<DocFeedbackControls {feedback} {compact} {withSend} />
