<script lang="ts">
	import { coreSettings, settingsLoadError, updateSettings } from '$lib/stores/settings';
	import { Toggle } from '$lib/components/ui';

	let { onError }: { onError: (message: string) => void } = $props();
	let saving = $state(false);
	const confirmed = $derived(typeof $coreSettings?.features?.focus_mode === 'boolean');

	async function setFocusMode(next: boolean) {
		onError('');
		saving = true;
		const result = await updateSettings({ features: { focus_mode: next } });
		saving = false;
		if (!result.ok) onError(result.error ?? 'Could not save Focus mode.');
	}
</script>

<div class="settings-card feature-row">
	<div class="feature-info">
		<span class="feature-name">Focus mode</span>
		<span class="feature-desc">
			{#if !confirmed}Loading saved Focus setting…
			{:else if $settingsLoadError}Could not refresh Focus setting; the last confirmed value is still shown.
			{:else}Show what agents say to you; hide their finished working steps behind a summary line. Turn off to see every step.{/if}
		</span>
	</div>
	<Toggle
		checked={$coreSettings?.features?.focus_mode === true}
		onchange={setFocusMode}
		ariaLabel="Focus mode"
		disabled={saving || !confirmed}
	/>
</div>

<style>
	.settings-card { margin-bottom: 16px; padding: 20px 24px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	.feature-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
	.feature-info { display: flex; flex: 1; flex-direction: column; gap: 3px; }
	.feature-name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: 500; }
	.feature-desc { color: var(--text-muted); font-size: var(--text-footnote); line-height: 1.5; }
	@media (max-width: 768px) { .feature-row { flex-direction: column; gap: 10px; } }
</style>
