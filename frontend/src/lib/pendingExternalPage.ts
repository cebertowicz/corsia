import { openExternalUrl } from '$lib/linkTargetResolver';

/** A browser page for a link a request has yet to return. `open` says whether a page showed it. */
export interface PendingExternalPage {
	/** The desktop app, where leaving the app window for the link is never a fallback. */
	readonly desktop: boolean;
	open(href: string): Promise<boolean>;
	close(): void;
}

/**
 * Call inside the click. On the web a blank tab opens now, while the browser still allows it, and is pointed at the
 * link once known; the desktop app has no tabs, so it hands the link to the default browser then.
 */
export function pendingExternalPage(): PendingExternalPage {
	if (document.documentElement.dataset.shell === 'tauri') {
		return { desktop: true, open: (href) => openExternalUrl(href).then(() => true, () => false), close: () => {} };
	}
	const page = window.open('', '_blank');
	return {
		desktop: false,
		async open(href) {
			if (!page) return false;
			page.opener = null;
			page.location.href = href;
			return true;
		},
		close: () => page?.close(),
	};
}
