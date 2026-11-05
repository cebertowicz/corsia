import { arrivedDocComments } from '$lib/stores/docComments';

/** The mounted card consumes its arrival only when it is visible. */
export function observeArrivedThread(
	card: () => HTMLElement | undefined,
	id: () => string,
	enabled: () => boolean,
	arrived: () => boolean,
	open: () => void,
	refresh: () => void,
	clear: () => void,
	cancel: () => void,
) {
	let visible = $state(false);
	let visibilityTick = $state(0);
	$effect(() => {
		const el = card();
		if (!el || !enabled()) return;
		const observer = new IntersectionObserver((entries) => { visible = entries.some((entry) => entry.isIntersecting); });
		observer.observe(el);
		return () => { observer.disconnect(); visible = false; };
	});
	$effect(() => {
		visibilityTick;
		if (!visible || document.visibilityState !== 'visible' || !arrived()) return;
		open();
		arrivedDocComments.update((ids) => { const remaining = new Set(ids); remaining.delete(id()); return remaining; });
	});
	$effect(() => () => clear());
	return () => {
		if (document.visibilityState === 'visible') { clear(); refresh(); visibilityTick++; }
		else cancel();
	};
}
