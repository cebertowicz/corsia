<script lang="ts">
	import { Button, Field } from '$lib/components/ui';
	import type { ConnectedAccount } from '$lib/api/connectedApps';
	import { retryMailHost, saveMailHostServers } from '$lib/api/mailHostAccounts';
	import { KEYS_NOT_SAVED, KeyWriteError } from '$lib/stores/keys';

	// The open row of a mail-host account (iCloud, Yahoo, a work server): what its last check found, and the one next step.
	// Try again is one press and re-checks the saved servers. Edit server decides where the app password goes, so Save asks for the CORSIA password (H17).
	// The row closes once a check starts: the result lands on its status line.
	let { account, offline = false, onSetKey, onWhere, onRemove, onChanged = () => {} }: {
		account: ConnectedAccount;
		offline?: boolean;
		onSetKey: () => void;
		onWhere: () => void;
		onRemove: () => void;
		/** A check started: the row closes and the list reloads, so it reads Checking…. */
		onChanged?: () => void;
	} = $props();
	const host = $derived(account.provider_display_name);
	const problem = $derived(account.mailhost?.problem ?? null);
	const server = $derived(account.mailhost?.server ?? '');
	let editing = $state(false);
	let imap = $state('');
	let smtp = $state('');
	let caldav = $state('');
	let password = $state('');
	let busy = $state(false);
	let passwordError = $state('');
	let formError = $state('');
	const reaches = $derived(account.capabilities.map((capability) => ({ mail: 'Email', calendar: 'Calendar', files: 'Files' }[capability])).join(', '));
	const detail = $derived.by(() => {
		switch (problem) {
			case 'waiting': return `${host} needs an app password before agents can use this account: a password just for CORSIA, made in your ${host} account settings. Press Set key, paste it and press Set.`;
			case 'refused': return `${host} didn’t accept this app password. Make a new one in your ${host} account settings, then press Replace key.`;
			case 'revoked': return `${host} stopped accepting the app password, so agents can’t use this account. ${host === 'iCloud' ? 'iCloud does this when your Apple password changes. ' : ''}Make a new one, then press Replace key.`;
			case 'imap_off': return `${host} has email access for other apps turned off. Turn on IMAP in ${host} Mail settings, then press Try again.`;
			case 'unreachable': return `CORSIA couldn’t reach ${server}. Check the server names with your host or IT.`;
			default: return '';
		}
	});
	const failing = $derived(account.mailhost?.failing);
	const dirty = $derived(imap !== (account.mailhost?.imap ?? '') || smtp !== (account.mailhost?.smtp ?? '') || caldav !== (account.mailhost?.caldav ?? ''));
	const IMAP_HINT = 'mail.example.com or mail.example.com:993';
	const SMTP_HINT = 'smtp.example.com or smtp.example.com:465';
	const UNREACHABLE = 'CORSIA couldn’t reach this server.';
	function reset(): void {
		editing = false; password = ''; passwordError = ''; formError = '';
	}
	function edit(): void {
		reset();
		imap = account.mailhost?.imap ?? ''; smtp = account.mailhost?.smtp ?? ''; caldav = account.mailhost?.caldav ?? '';
		editing = true;
	}
	/** Try again: one press, no password. A refusal reads as a line under the status. */
	async function tryAgain(): Promise<void> {
		if (busy) return;
		busy = true; formError = '';
		try {
			await retryMailHost(account.id);
		} catch (error) {
			formError = error instanceof KeyWriteError ? error.message : KEYS_NOT_SAVED;
			busy = false;
			return;
		}
		busy = false;
		onChanged();
	}
	/** Edit server's write: the password is cleared whatever the answer, a refusal reads on the field it belongs to. */
	async function submit(run: (typed: string) => Promise<void>): Promise<void> {
		if (busy) return;
		if (!password) { passwordError = 'Enter your CORSIA password.'; return; }
		const typed = password;
		password = '';
		busy = true; passwordError = ''; formError = '';
		try {
			await run(typed);
		} catch (error) {
			const message = error instanceof KeyWriteError ? error.message : KEYS_NOT_SAVED;
			if (error instanceof KeyWriteError && error.code === 'invalid_servers') formError = message; else passwordError = message;
			busy = false;
			return;
		}
		busy = false;
		reset();
		onChanged();
	}
	function save(): void {
		if (!dirty) return;
		void submit((typed) => saveMailHostServers(account.id, { imap, smtp, caldav }, typed));
	}
	// A filled primary button sits on the column edge; only a ghost button's padding is pulled out to it.
	const outdent = $derived(editing ? !dirty : problem === null || problem === 'checking');
</script>

{#if problem === null}<p class="capabilities"><span class="capability-label">Reaches</span><span>{reaches}</span></p>{/if}
{#if problem === null && account.mailhost?.calendar_missing}<p class="impact">{host} didn’t offer a calendar to CORSIA, so only email is connected.</p>{:else if detail}<p class="impact">{detail}</p>{/if}
{#if editing}
	<!-- Devices' outside-address pattern (SettingsDevicesView.svelte:107-115): a form of nested stacked Fields, Enter submits, Save only once a value has changed. Here Save and Cancel sit in the row's actions. -->
	<form class="server" onsubmit={(event) => { event.preventDefault(); save(); }}>
		<Field variant="nested" layout="stacked" label="Mail (IMAP) server" labelFor="mailhost-imap" hint={IMAP_HINT} error={failing === 'imap' && imap === (account.mailhost?.imap ?? '') ? UNREACHABLE : ''}><input id="mailhost-imap" type="text" autocapitalize="off" autocomplete="off" spellcheck="false" aria-invalid={failing === 'imap' && imap === (account.mailhost?.imap ?? '')} bind:value={imap} /></Field>
		<Field variant="nested" layout="stacked" label="Sending (SMTP) server" labelFor="mailhost-smtp" hint={SMTP_HINT} error={failing === 'smtp' && smtp === (account.mailhost?.smtp ?? '') ? UNREACHABLE : ''}><input id="mailhost-smtp" type="text" autocapitalize="off" autocomplete="off" spellcheck="false" aria-invalid={failing === 'smtp' && smtp === (account.mailhost?.smtp ?? '')} bind:value={smtp} /></Field>
		<Field variant="nested" layout="stacked" label="Calendar (CalDAV) address" labelFor="mailhost-caldav" error={failing === 'caldav' && caldav === (account.mailhost?.caldav ?? '') ? UNREACHABLE : ''}><input id="mailhost-caldav" type="text" autocapitalize="off" autocomplete="off" spellcheck="false" aria-invalid={failing === 'caldav' && caldav === (account.mailhost?.caldav ?? '')} bind:value={caldav} /></Field>
		{#if dirty}
			<Field variant="nested" layout="stacked" label="CORSIA password" labelFor="mailhost-password" error={passwordError}><input id="mailhost-password" type="password" autocomplete="current-password" aria-invalid={!!passwordError} bind:value={password} /></Field>
		{/if}
	</form>
{/if}
{#if formError}<p class="failed" role="alert">{formError}</p>{/if}
{#if offline}<p class="offline-action-note" role="status">Actions are unavailable while offline.</p>{/if}
{#if problem !== 'checking'}
	<div class="actions account-actions" class:outdent>
		{#if editing}
			{#if dirty}<span class="account-action"><Button variant="primary" size="sm" loading={busy} disabled={offline} onclick={save}>Save</Button></span>{/if}<span class="account-action"><Button variant="ghost" size="sm" disabled={offline || busy} onclick={reset}>Cancel</Button></span>
		{:else}
			{#if problem === 'waiting'}<span class="account-action"><Button variant="primary" size="sm" disabled={offline} onclick={onSetKey}>Set key</Button></span>
			{:else if problem === 'refused' || problem === 'revoked'}<span class="account-action"><Button variant="primary" size="sm" disabled={offline} onclick={onSetKey}>Replace key</Button></span>
			{:else if problem === 'imap_off'}<span class="account-action"><Button variant="primary" size="sm" loading={busy} disabled={offline} onclick={() => void tryAgain()}>Try again</Button></span>
			{:else if problem === 'unreachable'}<span class="account-action"><Button variant="primary" size="sm" disabled={offline || busy} onclick={edit}>Edit server</Button></span><span class="account-action"><Button variant="ghost" size="sm" loading={busy} disabled={offline} onclick={() => void tryAgain()}>Try again</Button></span>{/if}
			<span class="account-action"><Button variant="ghost" size="sm" disabled={offline || busy} onclick={onWhere}>Where it works…</Button></span>
			<span class="danger-text account-action"><Button variant="ghost" size="sm" disabled={offline || busy} onclick={onRemove}>Remove</Button></span>
		{/if}
	</div>
{/if}

<style>
	.capabilities { display: flex; gap: var(--space-2); margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.capability-label { color: var(--text-tertiary); }
	.impact { margin: 0 0 var(--space-2); color: var(--text-secondary); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.failed { margin: 0 0 var(--space-2); color: var(--red-text); font-size: var(--text-subhead); line-height: var(--lh-normal); }
	.offline-action-note { margin: 0 0 var(--space-2); color: var(--text-tertiary); font-size: var(--text-caption); line-height: var(--lh-normal); }
	.server { display: flex; flex-direction: column; gap: var(--space-2); max-width: calc(var(--space-8) * 9); margin: 0 0 var(--space-2); }
	.actions { display: flex; flex-wrap: wrap; gap: var(--space-1); }
	.actions.outdent { margin-left: calc(-1 * var(--button-pad-x-sm)); }
	.account-action { position: relative; display: inline-flex; }
	.account-action :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	.account-action::after { content: ''; position: absolute; inset: calc(-1 * var(--space-2)); }
	.account-action :global(button) { position: relative; z-index: 1; }
	.danger-text :global(.btn.ghost), .danger-text :global(.btn.ghost:hover:not(:disabled)) { color: var(--red-text); }
</style>
