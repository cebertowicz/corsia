import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

/** The desktop shell's way to save a setup report; absent in a browser. */
export const setupReportInvoke = () => typeof window === 'undefined' ? undefined : (window as Window & { __TAURI_INTERNALS__?: { invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;

/** One failed setup row, as the screen shows it. */
export type SetupReportRow = { key: 'claude-code' | 'codex' | 'omlx' | 'api'; name: string; cause?: string; reference?: string };

/** The service's allowlisted report body for these rows; '' when it can't be read, so the report still saves. */
export async function setupReportDetails(rows: SetupReportRow[]): Promise<string> {
	const session = get(token);
	try {
		const response = await fetch('/api/setup/report', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
			body: JSON.stringify({ rows: rows.map(({ key, name, cause, reference }) => ({ key, name, cause: cause || null, reference: reference || null })) }),
		});
		const body = response.ok ? await response.json() : null;
		return typeof body?.text === 'string' ? body.text : '';
	} catch {
		return '';
	}
}

/** Save the report; the error to show, or '' when it saved or there is nothing to save with. */
export async function saveSetupReport(rows: SetupReportRow[] = []): Promise<string> {
	const invoke = setupReportInvoke();
	if (!invoke) return '';
	try {
		await invoke('save_setup_report', { details: await setupReportDetails(rows) });
		return '';
	} catch {
		return 'CORSIA couldn’t save the report. Try again.';
	}
}
