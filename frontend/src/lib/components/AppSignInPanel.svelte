<script lang="ts">
	import { onDestroy, untrack } from 'svelte';
	import { Button } from '$lib/components/ui';
	import {
		appSignInStatus, cancelAppSignIn, SignInStartError, signOutOfApp, startAppSignIn,
		type ConnectedApp,
	} from '$lib/api/connectedApps';
	import {
		BROWSER_DIDNT_OPEN, failureText, NOT_ON_THIS_MAC, NO_RETRY, PROVIDER_NOT_TOLD_AFTER, PROVIDER_NOT_TOLD_BEFORE,
		SIGN_OUT_BODY, WAITING_TEXT,
	} from '$lib/appSignIn';

	interface Props {
		app: ConnectedApp;
		signInHere: boolean;
		confirmOut: boolean;
		onConfirmOut: (value: boolean) => void;
		onChange: (patch: Partial<ConnectedApp>) => void;
		onRefresh: () => void;
		onSignedOut: () => void;
		/** Set by the person's approve click when the approved app asks for a sign-in: start it once, as if Sign in were pressed. */
		autoStart?: boolean;
		onAutoStarted?: () => void;
	}
	let { app, signInHere, confirmOut, onConfirmOut, onChange, onRefresh, onSignedOut, autoStart = false, onAutoStarted }: Props = $props();

	const view = $derived(app.sign_in);
	const pending = $derived(view?.status === 'pending');
	const failed = $derived(view?.status === 'failed');
	let busy = $state(false);
	let error = $state('');
	let link = $state('');
	let blocked = $state(false);
	let copied = $state(false);
	let providerUrl = $state<string | null>(null);
	let words = $state('');
	let poll: ReturnType<typeof setInterval> | undefined;

	function patchView(next: Partial<NonNullable<ConnectedApp['sign_in']>>): void {
		onChange({ sign_in: { status: 'idle', signed_in: false, ...view, ...next } });
	}

	async function signIn(): Promise<void> {
		busy = true; error = ''; copied = false;
		// The page opens inside the click, before the request, so the browser allows it; it is pointed at the link once known.
		const page = signInHere ? window.open('', '_blank') : null;
		try {
			const started = await startAppSignIn(app.id);
			link = started.authorization_url;
			blocked = !page;
			if (page) { page.opener = null; page.location.href = link; }
			patchView({ status: 'pending', failure_class: undefined, expires_at: started.expires_at });
		} catch (cause) {
			page?.close();
			if (cause instanceof SignInStartError) patchView({ status: 'failed', failure_class: cause.failureClass });
			else error = cause instanceof Error ? cause.message : 'Could not start the sign-in. Try again.';
		} finally { busy = false; }
	}

	async function cancel(): Promise<void> {
		busy = true; error = '';
		try { await cancelAppSignIn(app.id); link = ''; patchView({ status: 'idle', failure_class: undefined }); }
		catch (cause) { error = cause instanceof Error ? cause.message : 'Could not cancel. Try again.'; }
		finally { busy = false; }
	}

	async function copyLink(): Promise<void> {
		try { await navigator.clipboard.writeText(link); copied = true; } catch { error = 'Could not copy the link.'; }
	}

	async function signOut(): Promise<void> {
		busy = true; error = '';
		try {
			const result = await signOutOfApp(app.id);
			onConfirmOut(false);
			providerUrl = result.provider_told ? null : result.provider_url;
			onChange({ state: result.state, tool_count: 0, sign_in: { status: 'idle', signed_in: false } });
			onSignedOut();
		} catch (cause) { error = cause instanceof Error ? cause.message : 'Could not sign out. Try again.'; }
		finally { busy = false; }
	}

	// Approving an app that needs a sign-in goes straight on to it; the blocked-page fallback covers a browser that refuses the tab.
	$effect(() => {
		if (!autoStart || app.state !== 'needs_sign_in' || !signInHere) return;
		onAutoStarted?.();
		untrack(() => void signIn());
	});

	// While a sign-in is pending the page asks the backend how it went; the row updates when the person finishes.
	$effect(() => {
		clearInterval(poll);
		if (pending) poll = setInterval(async () => {
			try { if ((await appSignInStatus(app.id)).status !== 'pending') onRefresh(); } catch { /* the next tick tries again */ }
		}, 2000);
		return () => clearInterval(poll);
	});
	onDestroy(() => clearInterval(poll));

	// An unknown failure shows the app's own words, which only the status route carries.
	$effect(() => {
		words = '';
		if (failed && view?.failure_class === 'unknown') appSignInStatus(app.id).then((status) => { words = status.words ?? ''; }).catch(() => {});
	});
</script>

{#if view}
	<div class="sign-in">
		{#if error}<p class="error" role="alert">{error}</p>{/if}
		{#if pending}
			<p>{WAITING_TEXT(app.name)}</p>
			{#if blocked}<p>{BROWSER_DIDNT_OPEN}</p>{/if}
		{:else if failed}
			<p>{failureText(app.name, view.failure_class ?? 'unknown')}</p>
			{#if words}<p class="said"><span>What {app.name} said</span> {words}</p>{/if}
		{:else if view.signed_in}
			<p><span class="label">Signed in as</span> {view.account ?? `${app.name} didn't say which account`}</p>
		{:else if app.state === 'needs_sign_in' && !signInHere}
			<p>{NOT_ON_THIS_MAC}</p>
		{/if}
		{#if providerUrl}
			<p>{PROVIDER_NOT_TOLD_BEFORE(app.name)}<a href={providerUrl} target="_blank" rel="noopener noreferrer">{app.name}'s settings</a>{PROVIDER_NOT_TOLD_AFTER}</p>
		{/if}
		{#if confirmOut}
			<p><strong>Sign out of {app.name}?</strong> {SIGN_OUT_BODY(app.name)}</p>
		{/if}
		<div class="actions">
			{#if pending}
				<Button variant="ghost" size="sm" loading={busy} onclick={signIn}>Open the page again</Button>
				{#if blocked && link}<Button variant="ghost" size="sm" onclick={copyLink}>{copied ? 'Copied' : 'Copy sign-in link'}</Button>{/if}
				<Button variant="ghost" size="sm" loading={busy} onclick={cancel}>Cancel</Button>
			{:else if failed}
				<Button variant="ghost" size="sm" loading={busy} onclick={signIn}>{NO_RETRY.has(view.failure_class ?? '') ? 'Sign in' : 'Try again'}</Button>
			{:else if view.signed_in}
				{#if confirmOut}
					<span class="danger-text"><Button variant="ghost" size="sm" loading={busy} onclick={signOut}>Sign out</Button></span>
					<Button variant="ghost" size="sm" onclick={() => onConfirmOut(false)}>Cancel</Button>
				{/if}
			{:else if app.state === 'needs_sign_in' && signInHere}
				<Button variant="ghost" size="sm" loading={busy} onclick={signIn}>Sign in</Button>
			{/if}
		</div>
	</div>
{/if}

<style>
	.sign-in { display: grid; gap: var(--space-2); margin-bottom: var(--space-3); }
	.sign-in p { margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.5; }
	.label { color: var(--text-tertiary); }
	.said span { color: var(--text-tertiary); }
	.error { color: var(--red); }
	.actions { display: flex; gap: var(--space-1); flex-wrap: wrap; margin-left: calc(-1 * var(--button-pad-x-sm)); }
	/* Signing out is a text action in the danger tone. */
	.danger-text :global(.btn.ghost), .danger-text :global(.btn.ghost:hover:not(:disabled)) { color: var(--red-text); }
</style>
