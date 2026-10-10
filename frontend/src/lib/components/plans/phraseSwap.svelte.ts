import { untrack } from 'svelte';
import { FADE_MS, holdRemaining } from './planLiveStep';

function reducedMotion(): boolean {
	return typeof window.matchMedia === 'function'
		&& window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/**
 * The one timing for a live phrase on screen: a new phrase waits out the shown one's
 * minimum hold, then crossfades in (skipped under reduced motion). Read by the plan row's
 * live step and by a Focus work block, so the two never drift apart. `between` rides
 * along with the phrase it belongs to and applies at once while the phrase is unchanged.
 */
export function createPhraseSwap(
	phrase: () => string,
	between: () => boolean = () => false,
): { readonly shown: string; readonly dimmed: boolean; readonly fading: boolean } {
	let shown = $state(untrack(phrase));
	let shownAt = untrack(() => Date.now());
	let dimmed = $state(untrack(between));
	let fading = $state(false);
	// Callers may hand over a new object per store emission; these primitives change only
	// when the phrase does, so an unrelated frame never restarts the swap below.
	const nextPhrase = $derived(phrase());
	const nextBetween = $derived(between());

	$effect(() => {
		const next = nextPhrase;
		const nextDim = nextBetween;
		if (next === untrack(() => shown)) {
			dimmed = nextDim;
			return;
		}
		let fadeTimer: ReturnType<typeof setTimeout> | undefined;
		const show = () => {
			shown = next;
			dimmed = nextDim;
			shownAt = Date.now();
			fading = false;
		};
		const holdTimer = setTimeout(() => {
			if (reducedMotion()) return show();
			fading = true;
			fadeTimer = setTimeout(show, FADE_MS);
		}, holdRemaining(shownAt, Date.now()));
		return () => {
			clearTimeout(holdTimer);
			clearTimeout(fadeTimer);
			fading = false;
		};
	});

	return {
		get shown() { return shown; },
		get dimmed() { return dimmed; },
		get fading() { return fading; },
	};
}
