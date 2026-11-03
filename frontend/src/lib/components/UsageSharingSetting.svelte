<script lang="ts">
	import { coreSettings, updateSettings } from '$lib/stores/settings';
	import { USAGE_SHARING_LABEL, USAGE_SHARING_LINE } from '$lib/usageSharing';
	import { Toggle } from '$lib/components/ui';
	import SettingsRow from './SettingsRow.svelte';

	let { onError }: { onError: (message: string) => void } = $props();
	let saving = $state(false);

	async function setSharing(next: boolean) {
		onError('');
		saving = true;
		const result = await updateSettings({ usage_sharing: next });
		saving = false;
		if (!result.ok) onError(result.error ?? 'Couldn’t save this setting. Try again.');
	}
</script>

<SettingsRow name={USAGE_SHARING_LABEL} sub={USAGE_SHARING_LINE} wrap>
	{#snippet actions()}
		<!-- The server owns the default; until its value arrives the switch shows nothing to change. -->
		<Toggle checked={$coreSettings?.usage_sharing === true} onchange={setSharing} ariaLabel={USAGE_SHARING_LABEL} disabled={saving || typeof $coreSettings?.usage_sharing !== 'boolean'} />
	{/snippet}
</SettingsRow>
