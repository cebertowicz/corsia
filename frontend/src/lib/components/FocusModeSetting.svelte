<script lang="ts">
	import { coreSettings, settingsLoadError, updateSettings } from '$lib/stores/settings';
	import { Toggle } from '$lib/components/ui';
	import SettingsRow from './SettingsRow.svelte';

	let { onError }: { onError: (message: string) => void } = $props();
	let saving = $state(false);
	const confirmed = $derived(typeof $coreSettings?.features?.focus_mode === 'boolean');
	const sub = $derived(
		!confirmed ? 'Loading saved Focus setting…'
		: $settingsLoadError ? 'Could not refresh Focus setting; the last confirmed value is still shown.'
		: 'Show what agents say to you; hide their finished working steps behind a summary line. Turn off to see every step.',
	);

	async function setFocusMode(next: boolean) {
		onError('');
		saving = true;
		const result = await updateSettings({ features: { focus_mode: next } });
		saving = false;
		if (!result.ok) onError(result.error ?? 'Could not save Focus mode.');
	}
</script>

<SettingsRow name="Focus mode" {sub} subTone={confirmed && $settingsLoadError ? 'failed' : 'quiet'} wrap>
	{#snippet actions()}
		<Toggle
			checked={$coreSettings?.features?.focus_mode === true}
			onchange={setFocusMode}
			ariaLabel="Focus mode"
			disabled={saving || !confirmed}
		/>
	{/snippet}
</SettingsRow>
