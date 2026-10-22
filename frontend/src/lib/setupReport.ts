import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

/** The desktop shell's way to save a setup report; absent in a browser. */
export const setupReportInvoke = () => typeof window === 'undefined' ? undefined : (window as Window & { __TAURI_INTERNALS__?: { invoke?: (command: string, args?: Record<string, unknown>) => Promise<unknown> } }).__TAURI_INTERNALS__?.invoke;

/** One failed setup row, as the screen shows it. */
export type SetupReportRow = { key: 'claude-code' | 'codex' | 'omlx' | 'api'; name: string; cause?: string; reference?: string };

/** Saved in place of the details when the service can't give them, so the report says what's missing. */
export const DETAILS_UNREAD = 'CORSIA couldn’t read the setup details from its service';

/** The service's allowlisted report body for these rows; a line saying why when it can't be read, so the report still saves. */
export async function setupReportDetails(rows: SetupReportRow[]): Promise<string> {
	const session = get(token);
	try {
		const response = await fetch('/api/setup/report', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
			body: JSON.stringify({ rows: rows.map(({ key, name, cause, reference }) => ({ key, name, cause: cause || null, reference: reference || null })) }),
		});
		if (!response.ok) return `${DETAILS_UNREAD} (HTTP ${response.status}).\n`;
		const body = await response.json().catch(() => null);
		return typeof body?.text === 'string' ? body.text : `${DETAILS_UNREAD} (unexpected answer).\n`;
	} catch {
		return `${DETAILS_UNREAD} (no answer).\n`;
	}
}

/** Save the report; the error to show, or '' when it saved or there is nothing to save with.
 * With no failed rows (an account or start-up failure) there are no row details to add. */
export async function saveSetupReport(rows: SetupReportRow[] = []): Promise<string> {
	const invoke = setupReportInvoke();
	if (!invoke) return '';
	try {
		await invoke('save_setup_report', { details: rows.length ? await setupReportDetails(rows) : '' });
		return '';
	} catch {
		return 'CORSIA couldn’t save the report. Try again.';
	}
}
