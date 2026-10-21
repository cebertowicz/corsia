/** The desktop shell's way to save a setup report; absent in a browser. */
export const setupReportInvoke = () => typeof window === 'undefined' ? undefined : (window as Window & { __TAURI_INTERNALS__?: { invoke?: (command: string) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;

/** Save the report; the error to show, or '' when it saved or there is nothing to save with. */
export async function saveSetupReport(): Promise<string> {
	const invoke = setupReportInvoke();
	if (!invoke) return '';
	try {
		await invoke('save_setup_report');
		return '';
	} catch {
		return 'CORSIA couldn’t save the report. Try again.';
	}
}
