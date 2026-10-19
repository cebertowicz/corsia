<script lang="ts">
	import { tick } from 'svelte';
	import { pendingCorsiaMessage, takeCorsiaMessage, type CorsiaMessageRequest } from '$lib/corsiaRequest';
	import { toasts } from '$lib/stores/toast';
	import type ChatPanel from './ChatPanel.svelte';

	let { goConductor, chatPanel, active }: {
		goConductor: () => Promise<boolean>;
		chatPanel: ChatPanel | undefined;
		active: () => boolean;
	} = $props();
	let inFlight = $state<string | null>(null);

	async function deliver(request: CorsiaMessageRequest): Promise<void> {
		try {
			if (!await goConductor() || !active()) {
				console.error('CORSIA request was not sent because its conversation could not open');
				toasts.show("Couldn't open CORSIA's chat. Try again.", 'error');
				return;
			}
			await tick();
			if (!active() || !chatPanel) {
				console.error('CORSIA request was not sent because its composer is unavailable');
				toasts.show("Couldn't open CORSIA's chat. Try again.", 'error');
				return;
			}
			if (!await chatPanel.setAndSend(request.text)) {
				console.warn('CORSIA request was not sent; its text remains in the composer');
			}
		} finally {
			if (!active()) return;
			takeCorsiaMessage(request.id);
			inFlight = null;
		}
	}

	$effect(() => {
		const request = $pendingCorsiaMessage;
		if (!request || inFlight) return;
		inFlight = request.id;
		void deliver(request);
	});
</script>
