<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { Button } from '$lib/components/ui';
	import {
		leaveDocFeedback,
		retryDocFeedback,
		returnDocFeedback,
		sendDocFeedback,
		type DocFeedbackResult,
		type UndeliveredBatch,
	} from '$lib/api/docFeedback';
	import type { CommentFeedback } from '$lib/api/docs';
	import { toasts } from '$lib/stores/toast';
	import { subscribeToDocumentCommentChanges } from '$lib/docViewerOperations';

	let {
		artifactId,
		targetInstanceId,
		onFeedbackChanged,
	}: {
		artifactId: string | null;
		targetInstanceId: string | null;
		onFeedbackChanged?: () => void;
	} = $props();

	let activeArtifactId = $state<string | null>(null);
	let activeTargetInstanceId = $state<string | null>(null);
	let pendingCount = $state(0);
	let undelivered = $state<UndeliveredBatch[]>([]);
	let sending = $state(false);
	let retrying = $state(false);
	let leaveSent = $state(false);
	let feedback = $state<CommentFeedback | null>(null);
	let countdownNow = $state(Date.now());

	const DELIVERY_REASONS: Record<string, string> = {
		no_owner: 'this document has no owner',
		owner_archived: 'its owner is archived',
		owner_unavailable: 'its owner is unavailable',
		owner_admission_refused: "its owner couldn't take it right now",
		owner_unaddressable: "its owner can't be reached",
	};
	const undeliveredCount = $derived(undelivered.reduce((count, batch) => count + batch.item_count, 0));
	const undeliveredReason = $derived.by(() => {
		const reasons = new Set(undelivered.map((batch) => batch.undelivered_reason));
		if (reasons.size !== 1) return '';
		const [reason] = reasons;
		return DELIVERY_REASONS[reason] ?? "it couldn't be delivered";
	});
	const undeliveredSummary = $derived(
		`${undeliveredCount} comment${undeliveredCount === 1 ? '' : 's'} not delivered${undeliveredReason ? `: ${undeliveredReason}` : ''}`,
	);
	const ownerName = $derived(feedback?.owner_label ?? null);
	const sendLabel = $derived(
		`Send ${pendingCount} comment${pendingCount === 1 ? '' : 's'}${ownerName ? ` to ${ownerName}` : ''}`,
	);
	const graceDeadline = $derived(feedback?.grace_closes_at ?? null);
	const graceMinutes = $derived.by(() => {
		if (!graceDeadline) return null;
		return Math.max(0, Math.ceil((Date.parse(graceDeadline) - countdownNow) / 60_000));
	});
	const sendingGrace = $derived(pendingCount > 0 && graceMinutes !== null);
	const graceText = $derived(`Sending to ${ownerName ?? 'the owner'} in ${graceMinutes} min`);

	$effect(() => {
		if (activeArtifactId === artifactId && activeTargetInstanceId === targetInstanceId) return;
		leaveCurrentDocument();
		activeArtifactId = artifactId;
		activeTargetInstanceId = targetInstanceId;
		pendingCount = 0;
		undelivered = [];
		leaveSent = false;
		feedback = null;
	});

	export function commentCreated() {
		pendingCount += 1;
		leaveSent = false;
	}

	export function sync(id: string, current: CommentFeedback) {
		if (activeArtifactId !== id) return;
		feedback = current;
		pendingCount = current.open_count;
		undelivered = current.undelivered_batches;
		if (pendingCount > 0) leaveSent = false;
		// Only a visible document counts as the user returning; a refresh in a hidden tab must not.
		if (document.visibilityState === 'visible' && !current.grace_closes_at) {
			void returnDocFeedback(id).catch((error) => console.error('feedback return failed', error));
		}
	}

	function applyResult(id: string, result: DocFeedbackResult) {
		if (activeArtifactId === id) undelivered = result.undelivered_batches;
	}

	async function sendCurrentDocument() {
		const id = activeArtifactId;
		if (!id || pendingCount === 0) return;
		sending = true;
		try {
			applyResult(id, await sendDocFeedback(id));
			if (activeArtifactId === id) pendingCount = 0;
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			sending = false;
		}
	}

	async function retryCurrentDocument() {
		const id = activeArtifactId;
		if (!id) return;
		retrying = true;
		try {
			applyResult(id, await retryDocFeedback(id));
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			retrying = false;
		}
	}

	function leaveCurrentDocument() {
		const id = activeArtifactId;
		if (!id || pendingCount === 0 || leaveSent) return;
		leaveSent = true;
		pendingCount = 0;
		void leaveDocFeedback(id).then((result) => applyResult(id, result)).catch((error) => {
			leaveSent = false;
			console.error('feedback leave failed', error);
		});
	}

	function onVisibilityChange() {
		if (document.visibilityState === 'hidden') leaveCurrentDocument();
		if (document.visibilityState === 'visible') onFeedbackChanged?.();
	}

	$effect(() => {
		if (!graceDeadline) return;
		const timer = setInterval(() => { countdownNow = Date.now(); }, 30_000);
		return () => clearInterval(timer);
	});

	onMount(() => subscribeToDocumentCommentChanges(
		() => activeArtifactId,
		() => onFeedbackChanged?.(),
	));
	onDestroy(leaveCurrentDocument);
</script>

<svelte:window onvisibilitychange={onVisibilityChange} onbeforeunload={leaveCurrentDocument} />

{#if pendingCount > 0}
	{#if sendingGrace}
		<span class="sending-now">
			<span class="sending-grace" role="status" title={graceText}>{graceText}</span>
			<Button size="sm" onclick={sendCurrentDocument} loading={sending}>Send now</Button>
		</span>
	{:else}
		<Button size="sm" onclick={sendCurrentDocument} loading={sending}>{sendLabel}</Button>
	{/if}
{/if}
<span class="undelivered" class:idle={undeliveredCount === 0}>
	<span role="status">{undeliveredCount > 0 ? undeliveredSummary : ''}</span>
	{#if undeliveredCount > 0}
		<Button size="sm" variant="secondary" onclick={retryCurrentDocument} loading={retrying}>Retry</Button>
	{/if}
</span>

<style>
	.sending-grace {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	.undelivered {
		display: inline-flex;
		flex-wrap: wrap;
		min-width: 0;
		max-width: 100%;
		align-items: center;
		gap: 4px;
		font-size: var(--text-footnote);
		color: var(--text-secondary);
	}
	.undelivered > span { flex: 1 1 12rem; min-width: 0; overflow-wrap: anywhere; }
	.undelivered :global(> .btn) { flex: none; min-height: 44px; }
	.undelivered :global(> .btn::after) { inset: 0; }
	/* Keep the live region mounted but out of the row's flow until it has text. */
	.undelivered.idle { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
</style>
