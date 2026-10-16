<script lang="ts">
	import { onMount } from 'svelte';
	import { createDevices } from '$lib/devices.svelte';
	import { shellInfo } from '$lib/remoteShell';
	import SettingsDevicesView from './SettingsDevicesView.svelte';

	/** The Let other devices connect row on Settings › System, fed by `GET`/`PUT /api/devices` and `POST /api/devices/code`. */
	const devices = createDevices({ remoteName: () => { const info = shellInfo(); return info?.mode === 'remote' ? info.name : undefined; } });
	onMount(() => devices.start());
</script>

<SettingsDevicesView view={devices.view} onToggle={(next) => void devices.setEnabled(next)} onNewCode={() => void devices.newCode()} onRetry={() => void devices.retry()} />
