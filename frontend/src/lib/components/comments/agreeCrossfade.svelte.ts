import { tick } from 'svelte';
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';
import { clipFade } from '$lib/actions/clipFade';
import { captureLayout, mentionHost, playLayoutCrossfade, type MentionPlayback } from '$lib/actions/layoutCrossfade';

/**
 * A card's Agree toggle under reduced motion: crossfade the card's layout instead of letting it jump.
 * Call during component setup; it returns the transition for the Agreed line.
 */
export function useAgreeCrossfade(card: () => HTMLElement | undefined, acked: () => boolean) {
	let previousAck: boolean | undefined;
	let playback: MentionPlayback | undefined;
	let generation = 0;
	$effect.pre(() => {
		const now = acked();
		const previous = previousAck; previousAck = now;
		const el = card();
		if (previous === undefined || previous === now || !el || !get(prefersReducedMotion)) return;
		const host = mentionHost(el);
		let capture = playback?.handoff();
		if (!capture) {
			const seen = playback?.supersede();
			capture = captureLayout(host);
			if (capture && seen?.midFade) Object.assign(capture, { from: seen.from, liveFrom: seen.liveFrom });
		}
		playback = undefined;
		if (!capture) return;
		const mine = ++generation;
		void tick().then(() => {
			if (mine !== generation || !host?.isConnected) { capture.overlay.remove(); return; }
			playback = playLayoutCrossfade(capture, host);
		});
	});
	$effect(() => () => { generation++; playback?.cancel(); });
	return (node: HTMLElement) => (get(prefersReducedMotion) ? { duration: 0 } : clipFade(node));
}
