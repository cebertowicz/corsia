type TauriWindow = Window & { __TAURI_INTERNALS__?: { invoke?: unknown } };

/** Whether this page can currently invoke the desktop bridge. */
export function hasTauriInvoke(target: Window = window): boolean {
	return typeof (target as TauriWindow).__TAURI_INTERNALS__?.invoke === 'function';
}

/** Marks the document as a Tauri shell before shell-dependent listeners are installed. */
export function installTauriShell(root: HTMLElement = document.documentElement, target: Window = window): () => void {
	if (!hasTauriInvoke(target)) return () => {};

	root.dataset.shell = 'tauri';
	const preventBrowserReload = (event: KeyboardEvent) => {
		if ((event.metaKey || event.ctrlKey) && (event.key === 'r' || event.key === 'R')) {
			event.preventDefault();
			const url = new URL(target.location.href);
			url.searchParams.set('_r', String(Date.now()));
			target.location.replace(url.toString());
		}
	};
	target.addEventListener('keydown', preventBrowserReload, true);
	return () => target.removeEventListener('keydown', preventBrowserReload, true);
}
