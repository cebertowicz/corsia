import { untrack } from 'svelte';
import { fromStore, get, writable } from 'svelte/store';
import { pushBackLayer } from '$lib/historyBackLayer';

/** True while the phone navigation is open, or being dragged open, over the app. HomeShell writes it. */
export const phoneNavOver = writable(false);

let closeNav: (() => void) | null = null;

/** HomeShell hands over how its navigation closes, so Back over a covered drawer closes the navigation. */
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
	// Back never dismisses a drawer the reader can't see: while it is covered Back closes the navigation, and the drawer
	// comes back as it was; the next Back closes it as before. Escape needs nothing here: the covered drawer is inert, so
	// its focus trap never hears it, and the page closes the navigation once every layer above it has had its turn.
	$effect(() => {
		if (!covers()) {
			const back = held;
			held = null;
			if (back?.isConnected && open()) back.focus({ preventScroll: true });
			return;
		}
		return pushBackLayer(() => {
			closeNav?.();
			return true;
		});
	});
	return {
		get covers() {
			return covers();
		},
	};
}
