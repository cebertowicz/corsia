<script lang="ts">
	import { onDestroy } from 'svelte';
	import {
		coreSettings,
		loadSettings,
		updateSettings,
		updateStatus,
		loadUpdateStatus,
	} from '$lib/stores/settings';
	import { readSystemVersion, restartSystem, waitForSystemRestart } from '$lib/stores/settingsSystemRestart';
	import { pickerModels, workspaceModels } from '$lib/stores/models';
	import { logout, token as authToken } from '$lib/stores/auth';
	import { themeSetting, type ThemeSetting } from '$lib/stores/theme';
	import { Button, StatePill, Toggle } from '$lib/components/ui';
	import FocusModeSetting from './FocusModeSetting.svelte';
	import { plainResponseError } from '$lib/plainError';
	import { reloadWithCacheBust } from '$lib/version';

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
	let hostAccessAutoApprovalSaving = $state(false);
	let restartMode = $state<'restart' | null>(null);
	let restartInProgress = $state(false);
	let updateConfirm = $state(false);
	let updateInProgress = $state(false);
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

	const updateAvailable = $derived($updateStatus?.available === true);
	const behindLabel = $derived.by(() => {
		const behind = $updateStatus?.commits_behind;
		if (behind == null) return 'New version available';
		return `${behind} commit${behind === 1 ? '' : 's'} behind`;
	});

	async function updateAndRestartServer() {
		updateInProgress = true;
		settingsError = '';
		startLifecycle('Preparing update…');
		try {
			const before = await readSystemVersion();
			const result = await restartSystem('update');
			if (!result.ok) {
				settingsError = result.message ?? 'Update failed before restart.';
				return;
			}
			lifecycleProgress = 'Draining active work…';
			const completed = await waitForSystemRestart(
				'update',
				before,
				result.operation_id,
				(message) => lifecycleProgress = message,
			);
			if (!completed.ok) {
				settingsError = completed.message ?? 'Update did not finish.';
				return;
			}
			updateConfirm = false;
			await loadUpdateStatus(true);
			reloadWithCacheBust();
		} finally {
			updateInProgress = false;
			finishLifecycle();
		}
	}

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
				mode,
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

	let accountConfirm = $state<'logout' | 'reset-url' | null>(null);
	let accountInProgress = $state(false);
	let isTauri = $state(false);
	$effect(() => {
		if (typeof window !== 'undefined') {
			isTauri = !!((window as any).__TAURI_INTERNALS__ || (window as any).__TAURI__);
		}
	});

	async function resetServerUrl() {
		const tauri = (window as any).__TAURI_INTERNALS__ ?? (window as any).__TAURI__;
		const invoke = tauri?.invoke ?? (window as any).__TAURI__?.core?.invoke;
		if (!invoke) {
			settingsError = 'Tauri runtime not available — open in the desktop shell.';
		return;
	}
	await invoke('reset_server_url');
	await logout();
	window.location.href = 'tauri://localhost';
	}

	function memoryColor(pressure: string): string {
		if (pressure === 'critical') return 'var(--red)';
		if (pressure === 'warning') return 'var(--yellow)';
		return 'var(--green)';
	}

	const readyModels = $derived($workspaceModels.filter(m => m.status === 'ready'));

	const hostAccessReviewerPhantom = $derived.by(() => {
		const id = $coreSettings?.host_access_reviewer_model ?? '';
		if (!id || readyModels.some(m => m.id === id)) return null;
		return { id, name: $coreSettings?.host_access_reviewer_model_name || id };
	});

	const hostAccessReviewerPill = $derived.by(() => {
		if ($coreSettings?.host_access_reviewer_status === 'ready') {
			return { state: 'success' as const, label: 'Ready' };
		}
		if ($coreSettings?.host_access_reviewer_status === 'unavailable') {
			return { state: 'error' as const, label: 'Unavailable' };
		}
		return { state: 'success' as const, label: "Requesting agent's model" };
	});

</script>

<div class="section-header">
	<h2>System</h2>
	<p>Backend controls and system health.</p>
</div>

{#if settingsError}
	<div class="settings-error">{settingsError}</div>
{/if}

<div class="settings-card theme-card">
	<span class="theme-label">Theme</span>
	<div class="theme-opts" role="radiogroup" aria-label="Theme">
		{#each themeOpts as o}
			<button class="theme-opt" class:active={$themeSetting === o.v} role="radio" aria-checked={$themeSetting === o.v} onclick={() => themeSetting.set(o.v)}>{o.l}</button>
		{/each}
	</div>
</div>

<FocusModeSetting onError={(message) => settingsError = message} />

<div class="settings-card" data-settings-anchor="host-access-review">
	<div class="card-title-row">
		<h3>Host Access Review</h3>
		<StatePill state={hostAccessReviewerPill.state} label={hostAccessReviewerPill.label} size="sm" />
	</div>
	<p class="card-desc">Independently reviews temporary access requests and each elevated action.</p>

	<div class="field-row">
		<label for="host-access-reviewer-model">Reviewer model</label>
		<select
			id="host-access-reviewer-model"
			value={$coreSettings?.host_access_reviewer_model ?? ''}
			onchange={async (e: Event) => {
				settingsError = '';
				const target = e.target as HTMLSelectElement;
				const result = await updateSettings({
					host_access_reviewer_model: target.value || null,
				});
				if (!result.ok) {
					target.value = $coreSettings?.host_access_reviewer_model ?? '';
					settingsError = result.error ?? 'Failed to update';
				}
			}}
		>
			<option value="">Same model as the requesting agent</option>
			{#if hostAccessReviewerPhantom}
				<option value={hostAccessReviewerPhantom.id}>{hostAccessReviewerPhantom.name} (unavailable)</option>
			{/if}
			{#each pickerModels($workspaceModels, $coreSettings?.host_access_reviewer_model) as model (model.id)}
				<option value={model.id}>{model.name}{model.desc ? ` (${model.desc})` : ''}</option>
			{/each}
		</select>
	</div>
	<div class="feature-row">
		<div class="feature-info">
			<span class="feature-name">Auto-approve leases</span>
			<span class="feature-desc">When the reviewer allows an application, activate it immediately without asking you. Per-action review and hard safety limits still apply. Repository-policy exceptions still require your approval.</span>
		</div>
		<Toggle
			checked={$coreSettings?.host_access_auto_approve ?? false}
			onchange={async (next) => {
				settingsError = '';
				hostAccessAutoApprovalSaving = true;
				const result = await updateSettings({ host_access_auto_approve: next });
				hostAccessAutoApprovalSaving = false;
				if (!result.ok) settingsError = result.error ?? 'Failed to update';
			}}
			ariaLabel="Automatically approve reviewed host-access leases"
			disabled={hostAccessAutoApprovalSaving}
		/>
	</div>
	{#if $coreSettings?.host_access_reviewer_message}
		<p class="settings-status" class:error={$coreSettings.host_access_reviewer_status === 'unavailable'}>
			{$coreSettings.host_access_reviewer_message}
		</p>
	{/if}
</div>

<div class="settings-card" data-settings-anchor="restart">
	<h3>Services</h3>
	<p class="card-desc">Restart drains and bounces the current build. Update pulls the current branch, checks dependencies, and rebuilds before restarting.</p>

	<div class="service-grid">
		<div class="service-row">
			<span class="service-name">Server</span>
			<span class="service-status up">Running</span>
			<div class="service-actions">
				{#if updateConfirm}
					<span class="confirm-text">Update + restart?</span>
					<Button variant="danger" size="sm" disabled={updateInProgress} onclick={updateAndRestartServer}>{updateInProgress ? 'Updating…' : 'Confirm'}</Button>
					<Button variant="ghost" size="sm" onclick={() => updateConfirm = false}>Cancel</Button>
				{:else if restartMode === 'restart'}
					<span class="confirm-text">Drain + restart?</span>
					<Button variant="danger" size="sm" disabled={restartInProgress} onclick={() => restartServer()}>{restartInProgress ? 'Draining…' : 'Confirm'}</Button>
					<Button variant="ghost" size="sm" onclick={() => restartMode = null}>Cancel</Button>
				{:else}
					{#if updateAvailable}
						<Button variant="primary" size="sm" onclick={() => updateConfirm = true}>Update</Button>
					{/if}
					<Button size="sm" onclick={() => restartMode = 'restart'}>Restart</Button>
				{/if}
			</div>
		</div>
		{#if lifecycleProgress}
			<div class="service-row" role="status" aria-live="polite">
				<span class="service-name">Restart progress</span>
				<span class="service-status">{lifecycleProgress} · {lifecycleElapsedSeconds}s elapsed</span>
				<div class="service-actions"></div>
			</div>
		{/if}
		{#if updateAvailable}
			<div class="service-row update-row">
				<span class="service-name">Update available</span>
				<span class="service-status">{behindLabel} on {$updateStatus?.branch}</span>
				<div class="service-actions"></div>
			</div>
		{:else if $updateStatus?.error}
			<div class="service-row">
				<span class="service-name">Update check</span>
				<span class="service-status">{$updateStatus.error}</span>
				<div class="service-actions"></div>
			</div>
		{/if}
	</div>
</div>

<div class="settings-card">
	<h3>Account</h3>
	<p class="card-desc">Sign out of this CORSIA instance, or {isTauri ? 'reset the desktop shell back to the server picker' : 'sign in again with a different token'}.</p>

	<div class="service-grid">
		<div class="service-row">
			<span class="service-name">Sign out</span>
			<span class="service-status">Clears your token and returns to the login screen on this server.</span>
			<div class="service-actions">
				{#if accountConfirm !== 'logout'}
					<Button size="sm" onclick={() => accountConfirm = 'logout'}>Log out</Button>
				{:else}
					<span class="confirm-text">Sign out?</span>
					<Button variant="danger" size="sm" disabled={accountInProgress} onclick={async () => { accountInProgress = true; try { await logout(); } finally { accountConfirm = null; accountInProgress = false; } }}>Confirm</Button>
					<Button variant="ghost" size="sm" onclick={() => accountConfirm = null}>Cancel</Button>
				{/if}
			</div>
		</div>
		{#if isTauri}
			<div class="service-row">
				<span class="service-name">Reset URL</span>
				<span class="service-status">Forgets this server and reopens the CORSIA picker.</span>
				<div class="service-actions">
					{#if accountConfirm !== 'reset-url'}
						<Button size="sm" onclick={() => accountConfirm = 'reset-url'}>Reset</Button>
					{:else}
						<span class="confirm-text">Reset and reopen picker?</span>
						<Button variant="danger" size="sm" disabled={accountInProgress} onclick={async () => {
							accountInProgress = true;
							try { await resetServerUrl(); }
							catch (e) { settingsError = String(e); }
							accountConfirm = null;
							accountInProgress = false;
						}}>{accountInProgress ? 'Resetting…' : 'Confirm'}</Button>
						<Button variant="ghost" size="sm" onclick={() => accountConfirm = null}>Cancel</Button>
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>

{#if systemStatus?.memory}
	<div class="settings-card">
		<h3>Resources</h3>
		<div class="status-grid">
			<div class="status-row">
				<span class="status-label">Memory</span>
				<span class="status-value">
					<span class="dot" style="background: {memoryColor(systemStatus.memory.pressure)}"></span>
					{systemStatus.memory.free_mb < 1024
						? systemStatus.memory.free_mb + 'MB'
						: (systemStatus.memory.free_mb / 1024).toFixed(1) + 'GB'} free / {systemStatus.memory.total_gb}GB
				</span>
			</div>
			{#if systemStatus.cpu}
				<div class="status-row">
					<span class="status-label">CPU</span>
					<span class="status-value">{systemStatus.cpu.usage_pct}%</span>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.section-header { margin-bottom: 24px; }
	.section-header h2 { font-size: var(--text-title2); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.section-header p { font-size: var(--text-subhead); color: var(--text-muted); margin: 0; }

	.settings-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 20px 24px; margin-bottom: 16px; }
	.theme-card { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
	.theme-label { font-size: var(--text-subhead); font-weight: var(--fw-medium); color: var(--text-primary); }
	.theme-opts { display: inline-flex; background: var(--bg-tertiary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 2px; gap: 2px; }
	.theme-opt { padding: 6px 16px; border-radius: var(--radius-xs); background: none; border: none; cursor: pointer; font-family: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); color: var(--text-secondary); transition: background var(--dur-tight), color var(--dur-tight); }
	.theme-opt:hover { color: var(--text-primary); }
	.theme-opt.active { background: var(--accent); color: #fff; }
	.settings-card h3 { font-size: var(--text-callout); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.card-title-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 4px; }
	.card-title-row h3 { min-width: 0; margin: 0; }
	.card-desc { font-size: var(--text-subhead); color: var(--text-muted); margin: 0 0 16px; line-height: 1.5; }
	.settings-status { margin: -2px 0 0; color: var(--text-secondary); font-size: var(--text-footnote); line-height: 1.45; }
	.settings-status.error { color: var(--red); }
	.settings-error { background: color-mix(in srgb, var(--red) 10%, transparent); border: 1px solid color-mix(in srgb, var(--red) 30%, transparent); color: var(--red); border-radius: var(--radius-xs); padding: 10px 14px; font-size: var(--text-subhead); margin-bottom: 16px; }
	.service-grid { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
	.service-row { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--bg-tertiary); border-radius: var(--radius-sm); }
	.service-name { font-size: var(--text-subhead); font-weight: 500; min-width: 70px; }
	.service-status { font-size: var(--text-footnote); color: var(--text-tertiary); }
	.service-status.up { color: var(--green); }
	.service-actions { margin-left: auto; display: flex; align-items: center; gap: 6px; }
	.card-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
	.confirm-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.confirm-text { font-size: var(--text-subhead); color: var(--text-secondary); }
	.field-row { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; }
	.field-row label { font-size: var(--text-footnote); font-weight: 500; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
	.field-row select { background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius-xs); color: var(--text-primary); font-size: var(--text-subhead); font-family: inherit; padding: 8px 12px; cursor: pointer; max-width: 100%; }
	.field-row select:focus { outline: none; border-color: var(--accent); }
	.status-grid { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; padding: 12px; background: var(--bg-primary); border-radius: var(--radius-xs); border: 1px solid var(--border-subtle); }
	.status-row { display: flex; align-items: center; gap: 12px; font-size: var(--text-subhead); }
	.status-label { color: var(--text-muted); width: 120px; flex-shrink: 0; }
	.status-value { color: var(--text-primary); display: flex; align-items: center; gap: 6px; }
	.dot { width: 7px; height: 7px; border-radius: var(--radius-circle); flex-shrink: 0; }

	.feature-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border-subtle); }
	.feature-row:last-child { border-bottom: none; }
	.feature-info { display: flex; flex-direction: column; gap: 3px; flex: 1; }
	.feature-name { font-size: var(--text-subhead); font-weight: 500; color: var(--text-primary); }
	.feature-desc { font-size: var(--text-footnote); color: var(--text-muted); line-height: 1.5; }

	@media (max-width: 768px) {
		.status-label { width: auto; min-width: 80px; }
		.service-row { flex-wrap: wrap; gap: 6px; }
		.service-actions { width: 100%; margin-left: 0; }

		.feature-row { flex-direction: column; gap: 10px; }
	}
</style>
