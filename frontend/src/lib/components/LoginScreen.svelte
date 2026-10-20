<script lang="ts">
	import { onMount } from 'svelte';
	import { token } from '$lib/stores/auth';
	import { connect } from '$lib/ws';
	import { connectionError } from '$lib/stores/connection';
	import { KEYS_SIGNED_OUT } from '$lib/stores/keys';
	import { Button } from '$lib/components/ui';
	import SetupShell from './SetupShell.svelte';
	import { saveSetupReport } from '$lib/setupReport';

	interface Props { onNoAccount?: () => void; }
	const { onNoAccount }: Props = $props();
	type Phase = 'checking' | 'no-account' | 'form' | 'error';
	let reportError = $state('');

	let phase = $state<Phase>('checking');
	let username = $state('');
	let password = $state('');
	let loginError = $state('');
	let submitting = $state(false);
	let checkingAgain = $state(false);

	// Setup is reachable only from the computer CORSIA runs on; the setup routes
	// answer 403 to anyone else, who keeps the `corsia user add` guidance.
	async function setupCapable(): Promise<boolean> {
		try {
			const response = await fetch('/api/setup/status');
			if (!response.ok && response.status !== 403 && response.status !== 409) throw new Error('setup status');
			return response.ok;
		} catch {
			phase = 'error';
			return false;
		}
	}

	async function loadStatus() {
		try {
			const res = await fetch('/api/auth/status');
			if (!res.ok) throw new Error(`status check failed (${res.status})`);
			const data = await res.json();
			phase = data.has_users ? 'form' : 'no-account';
			if (!data.has_users && (await setupCapable())) onNoAccount?.();
		} catch {
			phase = 'error';
		}
	}

	async function checkAgain() {
		checkingAgain = true;
		await loadStatus();
		checkingAgain = false;
	}

	async function handleSubmit() {
		if (submitting) return;
		loginError = '';
		submitting = true;
		try {
			const res = await fetch('/api/auth/login', {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ username: username.trim(), password }),
			});
			if (res.status === 401) {
				loginError = 'Wrong username or password.';
				password = '';
				return;
			}
			if (res.status === 429) {
				const body = await res.json().catch(() => null);
				loginError = typeof body?.detail === 'string'
					? body.detail
					: 'Too many attempts. Try again later.';
				return;
			}
			if (!res.ok) {
				loginError = `Sign-in failed (${res.status}).`;
				return;
			}
			const data = await res.json();
			token.set(data.token);
			connect();
		} catch {
			loginError = "Can't reach CORSIA.";
		} finally {
			submitting = false;
		}
	}

	function signInWithUrlToken(urlToken: string) {
		token.set(urlToken);
		connect();
	}

	$effect(() => {
		if ($connectionError === 'Invalid or expired session') {
			loginError = 'Your session ended. Sign in again.';
		}
		if ($connectionError === KEYS_SIGNED_OUT) loginError = KEYS_SIGNED_OUT;
	});

	onMount(() => {
		void loadStatus();
		const urlToken = new URLSearchParams(window.location.search).get('token');
		if (urlToken) {
			window.history.replaceState({}, '', window.location.pathname);
			signInWithUrlToken(urlToken);
		}
	});
</script>

{#if phase === 'error'}
	<SetupShell steps={[]} page="error" title="CORSIA couldn’t start" lede={reportError || 'Something went wrong while starting. Try again, or save a report to send.'} primary={{ label: 'Retry' }} onPrimary={checkAgain}>
		{#snippet secondary()}<Button variant="chip" onclick={async () => { reportError = await saveSetupReport(); }}>Save a report</Button>{/snippet}
	</SetupShell>
{:else}
<div class="login-screen">
	<div class="login-card">
		<img class="login-logo" src="/icons/corsia-mark.svg" alt="CORSIA" />
		<h1>CORSIA</h1>
		<p>Your personal AI assistant</p>
		{#if phase === 'no-account'}
			<p class="no-account-copy">No account exists yet. On the CORSIA computer, run:</p>
			<code class="cmd">corsia user add</code>
			<Button variant="primary" onclick={checkAgain} loading={checkingAgain} disabled={checkingAgain}>
				Check again
			</Button>
		{:else if phase === 'form'}
			{#if loginError}
				<div class="login-error">{loginError}</div>
			{/if}
			<form class="login-form" onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
				<input
					type="text"
					bind:value={username}
					autocomplete="username"
					autocapitalize="off"
					placeholder="Username"
					disabled={submitting}
				/>
				<input
					type="password"
					bind:value={password}
					autocomplete="current-password"
					placeholder="Password"
					disabled={submitting}
				/>
				<Button type="submit" variant="primary" loading={submitting} disabled={submitting}>
					Sign in
				</Button>
			</form>
		{/if}
	</div>
</div>
{/if}

<style>
	.login-screen {
		height: 100dvh; display: flex; align-items: center;
		justify-content: center; background: var(--bg-primary);
	}
	:global(html[data-shell="tauri"]) .login-screen {
		background: color-mix(in srgb, var(--bg-primary) 55%, transparent);
		backdrop-filter: blur(40px) saturate(180%);
		-webkit-backdrop-filter: blur(40px) saturate(180%);
	}
	.login-card { text-align: center; padding: 40px; }
	.login-logo {
		width: min(56vw, 240px);
		height: min(56vw, 240px);
		margin: 0 auto 28px;
		display: block;
		user-select: none;
		-webkit-user-drag: none;
		animation: hero-grow var(--dur-hero-plus) var(--ease-entrance) both;
		transform-origin: center;
	}
	@keyframes hero-grow {
		from { transform: scale(0.4); opacity: 0; }
		to { transform: scale(1); opacity: 1; }
	}
	.login-card h1,
	.login-card p,
	.login-card input,
	.login-card .cmd,
	.login-error {
		animation: fade-rise var(--dur-spring) var(--ease-entrance) var(--dur-standard) both;
	}
	.login-card :global(button) {
		animation: fade-rise var(--dur-spring) var(--ease-entrance) var(--dur-standard) both;
	}
	@keyframes fade-rise {
		from { opacity: 0; transform: translateY(6px); }
		to { opacity: 1; transform: translateY(0); }
	}
	.login-card h1 {
		font-size: var(--text-title1); font-weight: 600; margin-bottom: 8px;
		letter-spacing: -0.01em;
	}
	.login-card p { color: var(--text-secondary); margin-bottom: 28px; font-size: var(--text-body); }
	.no-account-copy { margin-bottom: 14px; }
	.login-form { display: contents; }
	.login-card input {
		width: 280px; padding: 12px 16px; background: var(--bg-tertiary);
		border: 1px solid var(--border); border-radius: var(--radius-sm);
		color: var(--text-primary); font-size: var(--text-input); outline: none;
		margin-bottom: 12px; display: block; margin-inline: auto;
	}
	.login-card input:focus { border-color: var(--accent); }
	.login-card input:disabled { opacity: 0.6; }
	.cmd {
		display: block;
		width: fit-content;
		margin: 0 auto 20px;
		padding: 8px 14px;
		background: var(--bg-tertiary);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono, 'SF Mono', ui-monospace, Menlo, monospace);
		font-size: var(--text-subhead);
		color: var(--text-primary);
		user-select: all;
	}
	.login-error {
		color: var(--red); font-size: var(--text-subhead); margin-bottom: 12px;
	}
</style>
