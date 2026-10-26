<script lang="ts">
	import { coreSettings, loadSettings, settingsLoadError, settingsLoading, updateSettings } from '$lib/stores/settings';
	import { Button, Toggle } from '$lib/components/ui';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';

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

<SettingsGroup label="System bulletins">
	<SettingsRow
		name="Check for maintainer bulletins"
		sub="When on, CORSIA checks a public file once a day for signed notes from its maintainers on System changes. The CORSIA agent reviews each note and is told to ask you before changing anything you decided. Turning this off does not stop the notice your agents get when an update changes System instructions."
		wrap
	>
		{#snippet actions()}
			<Toggle
				checked={$coreSettings?.live_bulletins?.enabled === true}
				onchange={setLiveBulletins}
				ariaLabel="Check for maintainer bulletins"
				disabled={saving || !known}
			/>
		{/snippet}
	</SettingsRow>
	{#if !known && $settingsLoadError}
		<p class="note failed" role="alert">
			<span>Could not read this setting.</span>
			<Button variant="ghost" size="sm" disabled={$settingsLoading} onclick={loadSettings}>Retry</Button>
		</p>
	{:else if !known}
		<p class="note" role="status">Loading saved setting…</p>
	{:else if $settingsLoadError}
		<p class="note failed" role="alert">
			<span>Could not refresh this setting. The switch shows the last confirmed value.</span>
			<Button variant="ghost" size="sm" disabled={$settingsLoading} onclick={loadSettings}>Retry</Button>
		</p>
	{/if}
	{#if error}
		<p class="note failed error" role="alert">{error}</p>
	{/if}
</SettingsGroup>

<style>
	/* Status and errors are text in the status colours, never a banner (DESIGN-SYSTEM §0.2). */
	.note { display: flex; align-items: center; gap: var(--space-2); margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }
	.note.failed { color: var(--red-text); }
</style>
