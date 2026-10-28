import { tick } from 'svelte';
import { on } from '$lib/ws';
import { toasts } from '$lib/stores/toast';
import { watchInputBarHeight } from './inputBarHelpers';
import type { createComposerDrafts } from './composerDrafts';

/** Subscribe the composer to delivery receipts and its size observer. */
export function watchComposer(inputArea: () => HTMLElement | undefined, threadId: () => string | null, drafts: ReturnType<typeof createComposerDrafts>): () => void {
	const stopHeight = watchInputBarHeight(inputArea, fn => { void tick().then(fn); });
	const unsubs = [
		on('message_added', data => drafts.settlePendingSubmit(data)),
		on('input_queued', data => drafts.settlePendingSubmit(data, 'queued')),
		on('input_accepted', data => drafts.settlePendingSubmit(data, 'delivered')),
		on('error', data => drafts.settlePendingSubmit(data, 'failed')),
		on('stop_receipt', data => {
			if (data?.thread_id !== threadId()) return;
			const text: string | undefined = {
				stopped: 'Turn stopped', already_terminal: 'That turn had already finished',
				stale_active: 'That turn had already ended — a newer one is running',
				no_active_turn: 'Nothing was running to stop',
			}[data?.outcome as string];
			if (text) toasts.show(text, data.outcome === 'stopped' ? 'success' : 'info');
		}),
	];
	return () => { stopHeight(); for (const unsubscribe of unsubs) unsubscribe(); };
}
