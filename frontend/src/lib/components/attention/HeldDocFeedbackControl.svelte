<script lang="ts">
	/**
	 * Send for document comments this thread holds back until someone sends them.
	 *
	 * The chat is the only place that can see every document the thread holds, so it offers the Send for the ones whose
	 * own header is not on screen. A document whose header Send is visible keeps that one sender (`visibleArtifactId`).
	 * Server counts decide what shows; nothing here predicts them.
	 */
	import { onMount } from 'svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import {
		heldDocFeedbackForThread,
		sendHeldDocFeedbackForThread,
		type HeldThreadFeedback,
	} from '$lib/api/docFeedback';
	import { chatSend, THREAD_SENT_EVENT } from '$lib/heldDocFeedbackScope';
	import { toasts } from '$lib/stores/toast';

	let { threadId, visibleArtifactId = null }: { threadId: string; visibleArtifactId?: string | null } = $props();

	let held = $state<HeldThreadFeedback | null>(null);
	let sending = $state(false);
	let loadSeq = 0;

	const send = $derived(chatSend(held, visibleArtifactId));

	async function read(id: string) {
		const seq = ++loadSeq;
		try {
			const current = await heldDocFeedbackForThread(id);
			// Only a real reading is shown: anything else (an error body, a proxy page) leaves what is there.
			if (seq === loadSeq && Array.isArray(current?.artifacts)) held = current;
		} catch {
			// A failed read leaves what is shown; the next change reads again.
		}
	}

	// Another thread starts with nothing, then reads its own.
	$effect(() => {
		const id = threadId;
		held = null;
		void read(id);
		return () => { loadSeq += 1; };
	});

	onMount(() => {
		const reread = () => void read(threadId);
		const visible = () => { if (document.visibilityState === 'visible') reread(); };
		window.addEventListener('doc-feedback-changed', reread);
		window.addEventListener('visibilitychange', visible);
		return () => {
			window.removeEventListener('doc-feedback-changed', reread);
			window.removeEventListener('visibilitychange', visible);
		};
	});

	async function sendAll() {
		if (sending) return;
		const id = threadId;
		sending = true;
		try {
			const result = await sendHeldDocFeedbackForThread(id);
			// The chat only sends documents whose canvases are off screen, so it has no canvas-local confirmation host.
			// Do not fall back to the global success-toast path; the sender clearing is the confirmation. What did not
			// go is said once, here, and left on its documents, whose headers carry the Retry.
			const failed = result.undelivered_count ?? 0;
			if (failed > 0) {
				toasts.show(`${failed} comment${failed === 1 ? '' : 's'} couldn’t be delivered. Open the document and choose Retry.`, 'error');
			}
			if (id === threadId) held = { open_count: result.open_count, artifacts: result.artifacts };
			// Open documents read what they still hold.
			window.dispatchEvent(new Event(THREAD_SENT_EVENT));
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			sending = false;
		}
	}
</script>

{#if send}
	<div class="held-doc-feedback" role="group" aria-label="Document comments waiting to be sent">
		<Button size="sm" onclick={() => void sendAll()} loading={sending}>{send.label}</Button>
	</div>
{/if}

<style>
	.held-doc-feedback {
		display: flex;
		justify-content: flex-end;
		min-width: 0;
	}
</style>
