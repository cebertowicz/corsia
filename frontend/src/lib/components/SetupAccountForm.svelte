<script lang="ts">
	import { Field } from '$lib/components/ui';
	interface Props {
		name: string; username: string; password: string; confirmation: string; email: string;
		usernameError: string; confirmationError: string; requiredUsername?: string | null;
		/** The account exists already: the username is its id and stays as made, and a blank password keeps the current one. */
		editing?: boolean;
		accountError: string; onSubmit: () => void; onValidateConfirmation: () => void;
	}
	let { name = $bindable(), username = $bindable(), password = $bindable(), confirmation = $bindable(),
		email = $bindable(), usernameError, confirmationError, requiredUsername, editing = false, accountError, onSubmit,
		onValidateConfirmation }: Props = $props();
	let suggested = $state('');
	$effect(() => {
		const next = name.toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 32);
		if (!requiredUsername && !editing && (!username || username === suggested)) username = next;
		suggested = next;
	});
</script>

<form class="form" onsubmit={(event) => { event.preventDefault(); onSubmit(); }}>
	<Field variant="chip" layout="stacked" label="Full name" labelFor="setup-name"><input id="setup-name" autocomplete="name" bind:value={name} /></Field>
	<Field variant="chip" layout="stacked" label="Username" labelFor="setup-username" error={usernameError || undefined}><input id="setup-username" autocomplete="username" autocapitalize="off" bind:value={username} readonly={!!requiredUsername || editing} /></Field>
	<div class="passwords">
		<Field variant="chip" layout="stacked" label="Password" labelFor="setup-password" error={password && password.length < 8 ? 'Use at least 8 characters.' : undefined}><input id="setup-password" type="password" autocomplete="new-password" bind:value={password} /></Field>
		<Field variant="chip" layout="stacked" label="Confirm password" labelFor="setup-confirm-password" error={confirmationError}><input id="setup-confirm-password" type="password" autocomplete="new-password" bind:value={confirmation} onblur={onValidateConfirmation} /></Field>
	</div>
	<Field variant="chip" layout="stacked" label="Email (optional)" labelFor="setup-email"><input id="setup-email" type="email" autocomplete="email" bind:value={email} /></Field>
	{#if accountError}<p class="error-text" role="alert">{accountError}</p>{/if}
</form>

<style>
	.form { display: flex; flex-direction: column; gap: var(--space-3); width: 100%; }
	.passwords { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-3); }
	.error-text { margin: 0; font-size: var(--text-subhead); }
	@media (max-width: 40em) { .passwords { grid-template-columns: 1fr; } }
</style>
