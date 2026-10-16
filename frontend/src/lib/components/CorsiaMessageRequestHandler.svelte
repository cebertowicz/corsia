<script lang="ts">
	import { tick } from 'svelte';
	import { pendingCorsiaMessage, takeCorsiaMessage, type CorsiaMessageRequest } from '$lib/corsiaRequest';
	import type ChatPanel from './ChatPanel.svelte';

	let { goConductor, chatPanel, active }: {
		goConductor: () => Promise<boolean>;
		chatPanel: ChatPanel | undefined;
		active: () => boolean;
	} = $props();
	let inFlight = $state<string | null>(null);

	async function deliver(request: CorsiaMessageRequest): Promise<void> {
		try {
			if (!await goConductor() || !active()) return;
			await tick();
			if (!active() || !chatPanel) return;
			await chatPanel.setAndSend(request.text);
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
