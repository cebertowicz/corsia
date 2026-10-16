<script lang="ts">
	import { Field, Message } from '$lib/components/ui';
	import FolderRow from './FolderRow.svelte';

	type FolderNotice = { kind: 'protected'; folder: string } | { kind: 'unwritable'; reason: string } | null;
	type FolderChoice = { state: 'ready'; path: string; protected_folder: string | null } | { state: 'unwritable'; path: string; reason: string };
	type TauriInvoke = (command: string, args?: Record<string, unknown>) => Promise<unknown>;
	interface Props {
		folder: string; folderChanged: boolean;
		name: string; username: string; password: string; confirmation: string; email: string;
		usernameError: string; confirmationError: string; requiredUsername?: string | null;
		accountError: string; accountBusy: boolean;
		folderChanging: boolean; folderNotice: FolderNotice;
		onSubmit: () => Promise<void>; onValidateConfirmation: () => void;
	}
	let {
		folder = $bindable(), folderChanged = $bindable(), name = $bindable(),
		username = $bindable(), password = $bindable(), confirmation = $bindable(),
		email = $bindable(), usernameError, confirmationError, requiredUsername, accountError,
		accountBusy, folderChanging = $bindable(), folderNotice = $bindable(), onSubmit,
		onValidateConfirmation,
	}: Props = $props();

	function tauriInvoke(): TauriInvoke | undefined {
		return (window as Window & { __TAURI_INTERNALS__?: { invoke?: TauriInvoke } }).__TAURI_INTERNALS__?.invoke;
	}

	async function changeFolder() {
		const invoke = tauriInvoke();
		if (!invoke || folderChanging) return;
		folderChanging = true;
		try {
			const choice = await invoke('choose_work_folder') as FolderChoice | null;
			if (!choice) return;
			folder = choice.path;
			folderChanged = true;
			folderNotice = choice.state === 'ready'
				? (choice.protected_folder ? { kind: 'protected', folder: choice.protected_folder } : null)
				: { kind: 'unwritable', reason: choice.reason };
		} catch {
			folderNotice = { kind: 'unwritable', reason: 'the folder picker could not check this folder' };
		} finally { folderChanging = false; }
	}

	async function submitAccount() {
		await onSubmit();
	}
</script>

<form class="form" onsubmit={(event) => { event.preventDefault(); void submitAccount(); }}>
	<div class="group">
		<Field variant="chip" layout="stacked" label="Name" labelFor="setup-name"><input id="setup-name" autocomplete="name" bind:value={name} /></Field>
		<Field variant="chip" layout="stacked" label="Username" labelFor="setup-username" error={usernameError || undefined}><input id="setup-username" autocomplete="username" autocapitalize="off" bind:value={username} readonly={!!requiredUsername} /></Field>
	</div>
	<div class="group">
		<Field variant="chip" layout="stacked" label="Password" labelFor="setup-password" error={password && password.length < 8 ? 'Use at least 8 characters.' : undefined}><input id="setup-password" type="password" autocomplete="new-password" bind:value={password} /></Field>
		<Field variant="chip" layout="stacked" label="Confirm password" labelFor="setup-confirm-password" error={confirmationError}><input id="setup-confirm-password" type="password" autocomplete="new-password" bind:value={confirmation} onblur={onValidateConfirmation} /></Field>
	</div>
	<div class="group"><Field variant="chip" layout="stacked" label="Email (optional)" labelFor="setup-email"><input id="setup-email" type="email" autocomplete="email" bind:value={email} /></Field></div>
	<FolderRow {folder} notice={folderNotice} changing={folderChanging} onChange={changeFolder} />
	{#if accountError}<Message variant="err">{accountError}</Message>{/if}
</form>

<style>
	.form { display: flex; flex-direction: column; gap: var(--space-3); max-width: calc(var(--space-8) * 8); }
	.group { display: flex; flex-direction: column; gap: var(--space-2); }
</style>
