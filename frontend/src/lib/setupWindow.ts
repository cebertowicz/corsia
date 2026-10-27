type Shell = { __TAURI_INTERNALS__?: { invoke?: (command: string) => Promise<unknown> } };

/** Setup is over. The desktop shell opens a normal-sized window for setup and grows one still at that size to fill
 * the screen here; a window the person resized is left alone. Does nothing in a browser. */
export async function finishSetupWindow(): Promise<void> {
	const invoke = typeof window === 'undefined' ? undefined : (window as Window & Shell).__TAURI_INTERNALS__?.invoke;
	if (!invoke) return;
	try {
		await invoke('finish_setup_window');
	} catch (error) {
		// The window just keeps the size it has; say why, so a missing grant is findable.
		console.warn('finish_setup_window failed', error);
	}
}
