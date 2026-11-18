import { tick } from 'svelte';

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), '
	+ 'textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

export function focusableElements(root: HTMLElement): HTMLElement[] {
	return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}

/**
 * Whether a closing surface may hand focus back: nothing holds focus, it fell to <body> or to an element that was
 * removed, or it is still inside the surface. Anywhere else the person moved it, and it stays where they put it.
 * The one rule for every closing overlay (Modal, focus traps, the comment sheet, the host-access control).
 */
export function focusStillOurs(surface: Element | null, active: Element | null): boolean {
	return !active || active === document.body || !active.isConnected || !!surface?.contains(active);
}

export interface FocusTrapOptions {
	/** Called when Escape is pressed inside the trap. */
	onEscape?: () => void;
	/** Skip moving focus into the trap on mount (rare — prefer the default). */
	autoFocus?: boolean;
}

/**
 * Confines Tab/Shift+Tab to one element, focuses it on mount, and restores the
 * previously focused element on unmount.
 *
 * Extracted from `Modal.svelte` so the bottom sheet gets exactly the same
 * keyboard behavior instead of a second, weaker implementation — the existing
 * anchored action-sheet pattern has no trap at all.
 */
export function focusTrap(node: HTMLElement, options: FocusTrapOptions = {}) {
	let opts = options;
	const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
	let mounted = true;

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			if (!opts.onEscape) return;
			// Escape belongs to the top trapped layer. Preventing the browser default
			// and stopping bubbling preserves sheet → artifact → app precedence.
			e.preventDefault();
			e.stopPropagation();
			opts.onEscape();
			return;
		}
		if (e.key !== 'Tab') return;
		const focusable = focusableElements(node);
		if (!focusable.length) {
			e.preventDefault();
			node.focus();
			return;
		}
		const first = focusable[0];
		const last = focusable[focusable.length - 1];
		const current = document.activeElement;
		if (e.shiftKey && (current === first || !node.contains(current))) {
			e.preventDefault();
			last.focus();
		} else if (!e.shiftKey && (current === last || !node.contains(current))) {
			e.preventDefault();
			first.focus();
		}
	}

	node.addEventListener('keydown', onKeydown);
	if (opts.autoFocus !== false) {
		void tick().then(() => {
			if (!mounted || !node.isConnected) return;
			(focusableElements(node)[0] ?? node).focus();
		});
	}

	return {
		update(next: FocusTrapOptions) {
			opts = next;
		},
		destroy() {
			mounted = false;
			node.removeEventListener('keydown', onKeydown);
			// Hand focus back only to a real control, and only if the person's focus is still
			// ours to give: on <body> or inside the trap. <body>.focus() blurs whatever holds
			// focus, and a trap that outlives an outro (the dock drawer's 260ms) would
			// otherwise pull focus off the control that already took it back.
			if (!focusStillOurs(node, document.activeElement)) return;
			if (previousFocus && previousFocus !== document.body && previousFocus.isConnected) previousFocus.focus();
		},
	};
}
