<script lang="ts">
	import { coreSettings, loadSettings, settingsLoadError, settingsLoading, updateSettings } from '$lib/stores/settings';
	import { Button, Toggle } from '$lib/components/ui';

	let saving = $state(false);
	let error = $state('');
	// The backend always reports the stored or default value; until it has, the
	// value is unknown and the switch stays disabled.
	const known = $derived(typeof $coreSettings?.live_bulletins?.enabled === 'boolean');

	async function setLiveBulletins(next: boolean) {
		error = '';
		saving = true;
		const result = await updateSettings({ live_bulletins: { enabled: next } });
		saving = false;
		if (!result.ok) error = result.error ?? 'Failed to update';
	}
</script>

<div class="settings-card">
	<h3>System bulletins</h3>
	<div class="feature-row">
		<div class="feature-info">
			<span class="feature-name">Check for maintainer bulletins</span>
			<span class="feature-desc">When on, CORSIA checks a public file once a day for signed notes from its maintainers about System changes. Your workspace conductor reviews each note and is told to ask you before changing anything you decided. Turning this off does not stop the notice your conductor gets when an update changes System instructions.</span>
		</div>
		<Toggle
			checked={$coreSettings?.live_bulletins?.enabled === true}
			onchange={setLiveBulletins}
			ariaLabel="Check for maintainer bulletins"
			disabled={saving || !known}
		/>
	</div>
	{#if !known && $settingsLoadError}
		<div class="settings-status" role="alert">
			<span>Could not read this setting.</span>
			<Button variant="ghost" size="sm" disabled={$settingsLoading} onclick={loadSettings}>Retry</Button>
		</div>
	{:else if !known}
		<div class="settings-status" role="status">Loading saved setting…</div>
	{:else if $settingsLoadError}
		<div class="settings-status" role="alert">
			<span>Could not refresh this setting. The switch shows the last confirmed value.</span>
			<Button variant="ghost" size="sm" disabled={$settingsLoading} onclick={loadSettings}>Retry</Button>
		</div>
	{/if}
	{#if error}
		<div class="settings-error" role="alert">{error}</div>
	{/if}
</div>

<style>
	.settings-card { margin-bottom: 16px; padding: 20px 24px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	.settings-card h3 { margin: 0 0 4px; color: var(--text-primary); font-size: var(--text-callout); font-weight: 600; }
	.feature-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
	.feature-info { display: flex; flex: 1; flex-direction: column; gap: 3px; }
	.feature-name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: 500; }
	.feature-desc { color: var(--text-muted); font-size: var(--text-footnote); line-height: 1.5; }
	.settings-status { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; color: var(--text-secondary); font-size: var(--text-footnote); }
	.settings-error { margin-top: 12px; padding: 10px 14px; border: 1px solid color-mix(in srgb, var(--red) 30%, transparent); border-radius: var(--radius-xs); background: color-mix(in srgb, var(--red) 10%, transparent); color: var(--red-text); font-size: var(--text-subhead); }
	@media (max-width: 768px) { .feature-row { flex-direction: column; gap: 10px; } }
</style>
