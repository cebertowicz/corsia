import { fromStore, get, writable } from 'svelte/store';

/** True while the phone navigation is open, or being dragged open, over the app. HomeShell writes it. */
export const phoneNavOver = writable(false);

/**
 * A drawer the phone navigation comes over is covered while it stays up: kept open with its place, out of sight and
 * reach, so the navigation under the finger takes the touch. A drawer opened while the navigation was already up is
 * left usable, so whatever the navigation itself opens is never hidden; that exemption ends when the navigation closes.
 */
export function coverUnderNav(open: () => boolean) {
	const over = fromStore(phoneNavOver);
	let wasOpen = open();
	let openedWithNav = $state(wasOpen && get(phoneNavOver));
	$effect.pre(() => {
		const isOpen = open();
		const navUp = over.current;
		if (isOpen && !wasOpen) openedWithNav = navUp;
		else if (!navUp) openedWithNav = false;
		wasOpen = isOpen;
	});
	return {
		get covers() {
			return open() && over.current && !openedWithNav;
		},
	};
}
