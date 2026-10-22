<script lang="ts">
	import { onMount } from 'svelte';
	import { createDevices } from '$lib/devices.svelte';
	import { shellInfo } from '$lib/remoteShell';
	import SettingsDevicesView from './SettingsDevicesView.svelte';

	interface Props {
		onConnectionState?: (enabled: boolean | null) => void;
	}
	let { onConnectionState = () => {} }: Props = $props();
	/** The Let other devices connect row on Settings › System, fed by `GET`/`PUT /api/devices` and `POST /api/devices/code`. */
	const devices = createDevices({ remoteName: () => { const info = shellInfo(); return info?.mode === 'remote' ? info.name : undefined; } });
	onMount(() => devices.start());
	// The code ran out: ask the host once what is true now (a new code, or none).
	$effect(() => { if (devices.view.endsInSeconds === 0) void devices.read(); });
	// Settings owns the mutually exclusive "Connect to another computer" entry point.
	$effect(() => { onConnectionState(devices.view.load === 'ready' ? devices.view.enabled : null); });
</script>

<SettingsDevicesView view={devices.view} onToggle={(next) => void devices.setEnabled(next)} onNewCode={() => void devices.newCode()} onRetry={() => void devices.retry()} onOutside={(text) => devices.setOutside(text)} />
