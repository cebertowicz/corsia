import { untrack } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import { pushBackLayer } from '$lib/historyBackLayer';

/** True while the phone navigation is open, or being dragged open, over the app. HomeShell writes it. */
export const phoneNavOver = writable(false);

let closeNav: (() => void) | null = null;

/** HomeShell hands over how its navigation closes, so Back or Escape over a covered drawer closes the navigation. */
export function closesPhoneNav(close: () => void): () => void {
	closeNav = close;
	return () => {
		if (closeNav === close) closeNav = null;
	};
}

/**
 * A drawer the phone navigation comes over is covered while it stays up: kept open with its place, out of sight and
 * reach, so the navigation under the finger takes the touch. A drawer opened while the navigation was already up is
 * left usable, so whatever the navigation itself opens is never hidden; that exemption ends when the navigation closes.
 */
export function coverUnderNav(open: () => boolean, panel: () => HTMLElement | null) {
	const over = fromStore(phoneNavOver);
	let wasOpen = open();
	let openedWithNav = $state(wasOpen && get(phoneNavOver));
	// What had focus in the drawer when the navigation came over it: covering takes focus away, and it goes back there.
	let held: HTMLElement | null = null;
	$effect.pre(() => {
		const isOpen = open();
		const navUp = over.current;
		if (isOpen && !wasOpen) openedWithNav = navUp;
		else if (!navUp) openedWithNav = false;
		wasOpen = isOpen;
		const active = document.activeElement;
		if (isOpen && navUp && !untrack(() => openedWithNav) && !held && active instanceof HTMLElement && panel()?.contains(active)) held = active;
	});
	const covers = () => open() && over.current && !openedWithNav;
	// Back and Escape never dismiss a drawer the reader can't see: while it is covered they close the navigation, and the
	// drawer comes back as it was; the next Back or Escape closes it as before.
	$effect(() => {
		if (!covers()) {
			const back = held;
			held = null;
			if (back?.isConnected && open()) back.focus({ preventScroll: true });
			return;
		}
		const escape = (event: KeyboardEvent) => {
			if (event.key !== 'Escape') return;
			event.preventDefault();
			event.stopPropagation();
			closeNav?.();
		};
		window.addEventListener('keydown', escape, true);
		const removeBackLayer = pushBackLayer(() => {
			closeNav?.();
			return true;
		});
		return () => {
			window.removeEventListener('keydown', escape, true);
			removeBackLayer();
		};
	});
	return {
		get covers() {
			return covers();
		},
	};
}
