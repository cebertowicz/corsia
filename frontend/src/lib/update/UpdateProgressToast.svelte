<script lang="ts">
	import { onDestroy } from 'svelte';
	import { toasts } from '$lib/stores/toast';
	import { elapsedSeconds, formatElapsed, isInFlight, stepMessage } from './model';
	import { updateScreenVisible } from './screenStore';
	import { updateOperation, updateProgressMinimized, updateServerClockOffsetMs, updateStarting, updateToastSuppressed, updateUnreachable } from './store';

	/**
	 * Headless: pins the update's one progress toast while an update runs (or its request is
	 * pending), ticks its elapsed time, and shrinks it to a pill and back. It draws nothing itself.
	 */
	let now = $state(Date.now());
	const tick = setInterval(() => { now = Date.now(); }, 1_000);
	onDestroy(() => clearInterval(tick));

	const operation = $derived($updateOperation && isInFlight($updateOperation) ? $updateOperation : null);
	const running = $derived(operation !== null || $updateStarting);
	const message = $derived(operation ? stepMessage(operation, $updateUnreachable) : 'Installing update');
	const elapsed = $derived(operation ? formatElapsed(elapsedSeconds(operation, now, $updateServerClockOffsetMs)) : '0:00');

	$effect(() => {
		if (!running) return;
		// The full-window screen and visible System Updates row are the two places that already carry this state.
		if ($updateScreenVisible || $updateToastSuppressed) {
			toasts.unpin('update');
			return;
		}
		if ($updateProgressMinimized) {
			toasts.pin('update', {
				type: 'progress', message: 'Updating', compact: true,
				expandLabel: 'Show update progress', onExpand: () => updateProgressMinimized.set(false),
			});
			return;
		}
		toasts.pin('update', {
			type: 'progress', message, time: elapsed,
			closeIcon: 'minimize', closeLabel: 'Minimize progress', onClose: () => updateProgressMinimized.set(true),
		});
	});
</script>
