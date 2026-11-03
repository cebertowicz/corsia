<script lang="ts">
	import { Field, ListRow, Toggle } from '$lib/components/ui';
	import { USAGE_SHARING_LABEL, USAGE_SHARING_LINE } from '$lib/usageSharing';
	import { EMAIL_HINT } from '$lib/setupWelcome';
	interface Props {
		name: string; username: string; password: string; confirmation: string; email: string; sharing: boolean;
		usernameError: string; confirmationError: string; requiredUsername?: string | null;
		/** The account exists already: the username is its id and stays as made, and a blank password keeps the current one. */
		editing?: boolean;
		/** The account is being saved with the switch's value, so the switch holds still until it is. */
		busy?: boolean;
		accountError: string; onSubmit: () => void; onValidateConfirmation: () => void;
	}
	let { name = $bindable(), username = $bindable(), password = $bindable(), confirmation = $bindable(),
		email = $bindable(), sharing = $bindable(), busy = false, usernameError, confirmationError, requiredUsername, editing = false, accountError, onSubmit,
		onValidateConfirmation }: Props = $props();
	// The username follows the first name until the person types in it; after that, even a cleared field stays cleared.
	// A username already there on arrival (Back to this step) counts as the person's own.
	let usernameTyped = !!username;
	$effect(() => {
		const first = name.trim().split(/\s+/)[0] ?? '';
		const next = first.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 32);
		if (!requiredUsername && !editing && !usernameTyped) username = next;
	});
</script>

<form class="form" onsubmit={(event) => { event.preventDefault(); onSubmit(); }}>
	<Field variant="chip" layout="stacked" label="Full name" labelFor="setup-name"><input id="setup-name" autocomplete="name" bind:value={name} /></Field>
	<Field variant="chip" layout="stacked" label="Username" labelFor="setup-username" error={usernameError || undefined}><input id="setup-username" autocomplete="username" autocapitalize="off" bind:value={username} oninput={() => (usernameTyped = true)} readonly={!!requiredUsername || editing} /></Field>
	<div class="passwords">
		<Field variant="chip" layout="stacked" label={editing ? 'New password (optional)' : 'Password'} labelFor="setup-password" error={password && password.length < 8 ? 'Use at least 8 characters.' : undefined}><input id="setup-password" type="password" autocomplete="new-password" bind:value={password} /></Field>
		<Field variant="chip" layout="stacked" label={editing ? 'Confirm new password' : 'Confirm password'} labelFor="setup-confirm-password" error={confirmationError}><input id="setup-confirm-password" type="password" autocomplete="new-password" bind:value={confirmation} onblur={onValidateConfirmation} /></Field>
	</div>
	<Field variant="chip" layout="stacked" label="Email (optional)" labelFor="setup-email" hint={EMAIL_HINT}><input id="setup-email" type="email" autocomplete="email" bind:value={email} /></Field>
	<div class="sharing">
		<ListRow name={USAGE_SHARING_LABEL} centered>
			{#snippet detail()}<span id="setup-sharing-line">{USAGE_SHARING_LINE}</span>{/snippet}
			{#snippet actions()}<Toggle size="sm" checked={sharing} ariaLabel={USAGE_SHARING_LABEL} describedBy="setup-sharing-line" disabled={busy} onchange={(next) => (sharing = next)} />{/snippet}
		</ListRow>
	</div>
	{#if accountError}<p class="error-text" role="alert">{accountError}</p>{/if}
</form>

<style>
	.form { display: flex; flex-direction: column; gap: var(--space-3); width: 100%; }
	.passwords { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
	/* A field that cannot be changed reads as plain text under its label, not as a box to type in. */
	.form :global(input[readonly]) { background: transparent; border-color: transparent; box-shadow: none; color: var(--text-secondary); cursor: default; }
	.sharing { font-size: var(--text-subhead); color: var(--text-secondary); line-height: var(--lh-normal); }
	.error-text { margin: 0; font-size: var(--text-subhead); }
	@media (max-width: 40em) { .passwords { grid-template-columns: 1fr; } }
</style>
