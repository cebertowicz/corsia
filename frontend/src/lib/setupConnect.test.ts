import { describe, expect, it } from 'vitest';

import { createSetupConnect, installCause, installFailureCopy, signinFailureCopy } from './setupConnect.svelte';

describe('setup CLI installer failure copy', () => {
	it.each([
		['site_refused', 'site_refused'],
		['connection', 'connection'],
		['download_unverified', 'download_unverified'],
		['installer_failed', 'installer_failed'],
	] as const)('keeps API code %s as the safe %s setup cause', (code, cause) => {
		expect(installCause(code)).toBe(cause);
	});

	it('names the maker whose site refused the download, with Retry and the report both kept', () => {
		expect(installFailureCopy('Codex', 'site_refused', 'cli-refused', 'OpenAI')).toEqual({
			meta: 'OpenAI’s site refused the download. Try again, or install it yourself.',
			reference: 'cli-refused',
			retry: true,
		});
	});

	it.each([
		['installer_failed', 'Couldn’t install Codex. Retry.'],
	] as const)('asks for no report on a %s failure with no reference to find', (cause, meta) => {
		expect(installFailureCopy('Codex', cause, null)).toEqual({ meta, reference: undefined, retry: true });
	});

	it('uses the approved connection recovery copy when the network is unavailable', () => {
		expect(installFailureCopy('Claude Code', 'connection', 'cli-offline')).toEqual({
			meta: 'Couldn’t download Claude Code. Check your internet connection, then Retry.',
			reference: 'cli-offline',
			retry: true,
		});
	});

	it('keeps the opaque reference on generic installer failure copy', () => {
		expect(installFailureCopy('Claude Code', 'download_unverified', 'cli-failed')).toEqual({
			meta: 'Couldn’t install Claude Code. Retry, or save a report.',
			reference: 'cli-failed',
			retry: true,
		});
	});

	it('calls a usage-limited Claude account subscription', () => {
		const flow = createSetupConnect({
			token: () => null,
			sources: () => [{ source: 'claude-code', name: 'Claude Code', state: 'ready', limited: true }],
			refreshSources: async () => undefined,
		});

		expect(flow.row({ source: 'claude-code', name: 'Claude Code', state: 'ready', limited: true }).meta)
			.toBe('Signed in, but your Claude subscription has reached its usage limit.');
	});
});

describe('unchecked setup accounts', () => {
	it('keeps fresh-install copy with no account lines', () => {
		const source = { source: 'claude-code', name: 'Claude Code', state: 'not_installed' as const, accounts: [] };
		const flow = createSetupConnect({ token: () => null, sources: () => [source], refreshSources: async () => undefined });
		expect(flow.row(source).meta).toBe('Uses your Claude subscription.');
	});
	it('keeps unverified reinstall and checking copy with no account lines', () => {
		const source = { source: 'claude-code', name: 'Claude Code', state: 'unverified' as const, accounts: [] };
		const flow = createSetupConnect({ token: () => null, sources: () => [source], refreshSources: async () => undefined });
		expect(flow.row({ ...source, cli_package_manager: 'homebrew' }).meta)
			.toBe('CORSIA can’t use this copy of Claude Code. Reinstall it in Terminal with brew reinstall claude-code, then come back here.');
		expect(flow.row({ ...source, reason: 'Checking Claude Code' })).toMatchObject({ meta: 'Checking Claude Code', busy: true });
	});
	it.each([['claude-code', 'Claude Code', 'Claude'], ['codex', 'Codex', 'ChatGPT']] as const)('%s offers recovery for unchecked models', (vendor, name, accountName) => {
		const source = { source: vendor, name, state: 'needs_auth' as const, accounts: [{ account_id: 'account', label: accountName, label_verified: true, identified: true, state: 'unchecked' as const, resets_at: null, corsia_agent_binding: false }] };
		const flow = createSetupConnect({ token: () => null, sources: () => [source], refreshSources: async () => undefined });
		const row = flow.row(source);
		expect(row.meta).toBe(`Couldn’t check your ${accountName} models. Sign in again.`);
		expect(row).toMatchObject({ error: true, canTurnOff: true, action: { label: 'Sign in' } });
		expect(row.meta).not.toContain('Uses your');
	});
	it('keeps a ready account as Ready when another account is unchecked', () => {
		const account = { account_id: 'account', label: 'Claude', label_verified: true, identified: true, resets_at: null, corsia_agent_binding: false };
		const source = { source: 'claude-code', name: 'Claude Code', state: 'ready' as const, accounts: [{ ...account, state: 'unchecked' as const }, { ...account, account_id: 'ready', state: 'ready' as const }] };
		const flow = createSetupConnect({ token: () => null, sources: () => [source], refreshSources: async () => undefined });
		expect(flow.row(source)).toMatchObject({ meta: 'Signed in as Claude', ready: true });
	});
});

describe('setup sign-in failure copy', () => {
	it('never puts an unknown cause code into the sentence', () => {
		const copy = signinFailureCopy('codex', 'some_new_cause', 'signin-0123456789abcdef');
		expect(copy).toEqual({ meta: 'Sign-in didn’t finish. Press Sign in to try again, or save a report.', reportable: true });
	});

	it('asks for a report only when a reference gives it something to find', () => {
		expect(signinFailureCopy('codex', 'some_new_cause').meta).toBe('Sign-in didn’t finish. Press Sign in to try again.');
		expect(signinFailureCopy('codex', 'agent_unavailable').meta).toBe('Sign-in didn’t finish. Press Sign in to try again.');
	});

	it('keeps CORSIA’s own failures to one line and still offers the report', () => {
		for (const cause of ['agent_unavailable', 'unreachable', 'prompt_unrecognized', 'signin_check_failed']) {
			expect(signinFailureCopy('codex', cause).meta).toBe('Sign-in didn’t finish. Press Sign in to try again.');
			expect(signinFailureCopy('codex', cause).reportable).toBe(true);
		}
	});

	it('says the browser never finished when it was closed, and what happened when it never came back', () => {
		expect(signinFailureCopy('codex', 'vendor_login_failed')).toEqual({ meta: 'Sign-in didn’t finish in your browser. Press Sign in to try again.', reportable: false });
		expect(signinFailureCopy('codex', 'signin_timed_out')).toEqual({ meta: 'Couldn’t finish signing in to Codex. If your browser said this account doesn’t include Codex, sign in with another account or turn Codex off. Otherwise, sign in again.', reportable: false });
	});

	it('tells a person who signed in to the same account what to do next', () => {
		expect(signinFailureCopy('claude-code', 'same_account').meta).toContain('sign in again with a different Claude account');
	});
});
