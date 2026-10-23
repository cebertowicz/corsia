type TauriWindow = Window & { __TAURI_INTERNALS__?: unknown; __TAURI__?: unknown };

/** Marks the document as a Tauri shell before shell-dependent listeners are installed. */
export function installTauriShell(root: HTMLElement = document.documentElement, target: Window = window): () => void {
	const shell = target as TauriWindow;
	if (shell.__TAURI_INTERNALS__ === undefined && shell.__TAURI__ === undefined) return () => {};

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
