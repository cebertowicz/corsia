import { write as keyWrite } from '$lib/stores/keys';
import type { ConnectedAccount } from './connectedApps';

/** The row status line of a mail-host account, as Connected apps shows it. */
export function mailHostStateLine(account: Pick<ConnectedAccount, 'provider_display_name' | 'mailhost'>): string {
	switch (account.mailhost?.problem ?? null) {
		case 'waiting': return 'Needs an app password';
		case 'checking': return 'Checking…';
		case 'refused': case 'revoked': return 'App password not accepted';
		case 'imap_off': return `Email access is off at ${account.provider_display_name}`;
		case 'unreachable': return `Can’t reach ${account.mailhost?.server}`;
		default: return 'Connected';
	}
}

/** Try again: one press, nothing but the row's id (the server reads the saved servers and needs a signed-in person, not a password). Throws `KeyWriteError`. */
export async function retryMailHost(id: string): Promise<void> {
	await keyWrite(`/api/mcp/accounts/${encodeURIComponent(id)}/mailhost/check`, 'POST', {});
}

/** Edit server: the person-only write that decides where the app password goes, so it carries the CORSIA password (H17). */
export async function saveMailHostServers(id: string, servers: { imap: string; smtp: string; caldav: string }, password: string): Promise<void> {
	await keyWrite(`/api/mcp/accounts/${encodeURIComponent(id)}/mailhost/servers`, 'PUT', { ...servers, password });
}
