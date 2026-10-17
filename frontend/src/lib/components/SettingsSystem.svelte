<script lang="ts">
	import { onDestroy } from 'svelte';

	import { readSystemVersion, restartSystem, waitForSystemRestart } from '$lib/stores/settingsSystemRestart';
	import { logout } from '$lib/stores/auth';
	import { themeSetting, type ThemeSetting } from '$lib/stores/theme';
	import { Button } from '$lib/components/ui';
	import FocusModeSetting from './FocusModeSetting.svelte';
	import SettingsUpdate from './SettingsUpdate.svelte';
	import { updateInFlight } from '$lib/update/store';
	import HostAccessReviewSetting from './HostAccessReviewSetting.svelte';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';

	const themeOpts: { v: ThemeSetting; l: string }[] = [
		{ v: 'light', l: 'Light' },
		{ v: 'dark', l: 'Dark' },
		{ v: 'auto', l: 'Auto' },
	];

	let { systemStatus = {} as any, onSwitchSection }: {
		systemStatus?: { memory?: any; cpu?: any };
		onSwitchSection?: (s: string) => void;
	} = $props();

	let settingsError = $state('');
	let restartMode = $state<'restart' | null>(null);
	let restartInProgress = $state(false);

	let lifecycleProgress = $state('');
	let lifecycleElapsedSeconds = $state(0);
	let lifecycleTimer: ReturnType<typeof setInterval> | null = null;

	function startLifecycle(message: string) {
		if (lifecycleTimer !== null) clearInterval(lifecycleTimer);
		const startedAt = Date.now();
		lifecycleProgress = message;
		lifecycleElapsedSeconds = 0;
		lifecycleTimer = setInterval(() => {
			lifecycleElapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);
		}, 1_000);
	}

	function finishLifecycle() {
		if (lifecycleTimer !== null) clearInterval(lifecycleTimer);
		lifecycleTimer = null;
		lifecycleProgress = '';
		lifecycleElapsedSeconds = 0;
	}

	onDestroy(finishLifecycle);


	async function restartServer(mode: 'restart' = 'restart') {
		restartInProgress = true;
		settingsError = '';
		startLifecycle('Preparing restart…');
		try {
			const before = await readSystemVersion();
			const result = await restartSystem(mode);
			if (!result.ok) {
				settingsError = result.message ?? 'Restart could not be started.';
				return;
			}
			lifecycleProgress = 'Draining active work…';
			const completed = await waitForSystemRestart(
				before,
				result.operation_id,
				(message) => lifecycleProgress = message,
			);
			if (!completed.ok) {
				settingsError = completed.message ?? 'Restart did not finish.';
				return;
			}
			restartMode = null;
		} finally {
			restartInProgress = false;
			finishLifecycle();
		}
	}

	let accountConfirm = $state<'logout' | null>(null);
	let accountInProgress = $state(false);

	async function signOut() {
		accountInProgress = true;
		try { await logout(); } finally { accountConfirm = null; accountInProgress = false; }
	}

	const serverLine = $derived(
		lifecycleProgress ? `${lifecycleProgress} · ${lifecycleElapsedSeconds}s elapsed`
		: $updateInFlight ? 'Running · Unavailable while an update runs'
		: 'Running',
	);

	function memoryFree(memory: { free_mb: number; total_gb: number }): string {
		const free = memory.free_mb < 1024 ? `${memory.free_mb}MB` : `${(memory.free_mb / 1024).toFixed(1)}GB`;
		return `${free} free / ${memory.total_gb}GB`;
	}

	// Pressure is coloured text on the row, in the status colours (§0.1); normal pressure says nothing.
	const memoryPressure = $derived(systemStatus?.memory?.pressure as string | undefined);

</script>

{#if settingsError}
	<p class="error" role="alert">{settingsError}</p>
{/if}

<SettingsGroup label="Display">
	<SettingsRow name="Theme">
		{#snippet actions()}
			<div class="theme-opts" role="radiogroup" aria-label="Theme">
				{#each themeOpts as o}
					<button class="theme-opt" class:active={$themeSetting === o.v} role="radio" aria-checked={$themeSetting === o.v} onclick={() => themeSetting.set(o.v)}>{o.l}</button>
				{/each}
			</div>
		{/snippet}
	</SettingsRow>
	<FocusModeSetting onError={(message) => settingsError = message} />
</SettingsGroup>

<HostAccessReviewSetting onError={(message) => settingsError = message} />

<SettingsGroup label="Updates" anchor="update">
	<SettingsUpdate />
</SettingsGroup>

<SettingsGroup label="Services" anchor="restart" hint="Restart drains and bounces the current build.">
	<SettingsRow name="Server" sub={serverLine}>
		{#snippet actions()}
			{#if restartMode === 'restart'}
				<span class="confirm-text">Drain + restart?</span>
				<Button variant="ghost" size="sm" disabled={restartInProgress} onclick={() => restartServer()}>{restartInProgress ? 'Draining…' : 'Confirm'}</Button>
				<Button variant="ghost" size="sm" onclick={() => restartMode = null}>Cancel</Button>
			{:else}
				<Button variant="ghost" size="sm" disabled={$updateInFlight} title={$updateInFlight ? 'Restart is unavailable while CORSIA updates.' : undefined} onclick={() => restartMode = 'restart'}>Restart</Button>
			{/if}
		{/snippet}
	</SettingsRow>
	<span class="live" role="status" aria-live="polite">{lifecycleProgress}</span>
</SettingsGroup>

<SettingsGroup label="Account">
	<SettingsRow name="Sign out" sub="Clears your token and returns to the login screen on this server." wrap>
		{#snippet actions()}
			{#if accountConfirm !== 'logout'}
				<Button variant="ghost" size="sm" onclick={() => accountConfirm = 'logout'}>Log out</Button>
			{:else}
				<span class="confirm-text">Sign out?</span>
				<Button variant="ghost" size="sm" disabled={accountInProgress} onclick={signOut}>Confirm</Button>
				<Button variant="ghost" size="sm" onclick={() => accountConfirm = null}>Cancel</Button>
			{/if}
		{/snippet}
	</SettingsRow>
</SettingsGroup>

{#if systemStatus?.memory}
	<SettingsGroup label="Resources">
		<SettingsRow
			name="Memory"
			trailing={memoryFree(systemStatus.memory)}
			sub={memoryPressure === 'critical' || memoryPressure === 'warning' ? `Memory pressure: ${memoryPressure}` : ''}
			subTone={memoryPressure === 'critical' ? 'failed' : memoryPressure === 'warning' ? 'attention' : 'quiet'}
		/>
		{#if systemStatus.cpu}
			<!-- The backend reports {error} instead of a figure when it cannot read CPU use. -->
			<SettingsRow name="CPU" trailing={typeof systemStatus.cpu.usage_pct === 'number' ? `${systemStatus.cpu.usage_pct}%` : 'Unavailable'} />
		{/if}
	</SettingsGroup>
{/if}

<style>
	.error { margin: 0 0 var(--space-4); padding: 0 var(--space-2); font-size: var(--text-footnote); color: var(--red-text); }
	/* Restart progress is announced on its own; the row's second line shows it. */
	.live { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
	.confirm-text { font-size: var(--text-caption); color: var(--text-secondary); }
	/* Theme stays the inline three-way control until the SegmentedControl primitive lands (DESIGN-SYSTEM §4.1);
	   the chosen option takes the selected-row wash (§0.1), not a solid accent fill. */
	.theme-opts { display: inline-flex; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 2px; gap: 2px; }
	.theme-opt { padding: var(--space-1) var(--space-4); border-radius: var(--radius-xs); background: none; border: none; cursor: pointer; font-family: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); color: var(--text-secondary); transition: background var(--dur-tight), color var(--dur-tight); }
	.theme-opt:hover { color: var(--text-primary); }
	.theme-opt.active { background: var(--accent-wash); color: var(--accent-text); }
	.theme-opt:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
</style>
