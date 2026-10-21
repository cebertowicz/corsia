<script lang="ts">
	import { onMount } from 'svelte';
	import { coreSettings } from '$lib/stores/settings';
	import {
		myBuild,
		serverVersion,
		isFrontendStale,
		isBackendBehindOnDisk,
		loadServerVersion,
		formatHHMM,
	} from '$lib/version';

	onMount(() => {
		loadServerVersion();
		const id = setInterval(loadServerVersion, 60_000);
		return () => clearInterval(id);
	});

	// The backend is the canonical owner of this default: it distinguishes an
	// app bundle (off) from a source checkout (on). Do not show a developer
	// build stamp while that resolved feature snapshot is still unavailable.
	const visible = $derived(
		$coreSettings?.features?.debug_version_overlay === true,
	);
	const backendShort = $derived($serverVersion?.backend.short_hash ?? '·······');
	const backendDirty = $derived($serverVersion?.backend.dirty);
	const diskHeadShort = $derived(
		$serverVersion?.backend.current_head?.short_hash ?? '·······',
	);
	const serverFrontendShort = $derived(
		$serverVersion?.frontend.short_hash ?? '·······',
	);
	const frontendStale = $derived($isFrontendStale);
	const backendBehind = $derived($isBackendBehindOnDisk);
	const stale = $derived(frontendStale || backendBehind);
	const timeText = formatHHMM(myBuild.build_iso);
</script>

{#if visible}
	<div class="version-overlay" class:stale aria-hidden="true">
		{#if backendBehind}
			<span class="seg">b {backendShort}<span class="arrow">⇢</span>{diskHeadShort}</span>
		{:else}
			<span class="seg">b {backendShort}{backendDirty === null ? '?' : backendDirty ? '†' : ''}</span>
		{/if}
		<span class="dot">·</span>
		{#if frontendStale}
			<span class="seg">f {myBuild.short_hash}<span class="arrow">⇢</span>{serverFrontendShort}</span>
		{:else}
			<span class="seg">f {myBuild.short_hash}{myBuild.dirty === null ? '?' : myBuild.dirty ? '†' : ''}</span>
		{/if}
		<span class="dot">·</span>
		<span class="seg">{timeText}</span>
	</div>
{/if}

<style>
	.version-overlay {
		position: fixed;
		top: max(4px, calc(var(--safe-top) + 2px));
		left: 50%;
		transform: translateX(-50%);
		z-index: 11;
		pointer-events: none;
		font-family: 'SF Mono', ui-monospace, Menlo, monospace;
		font-size: var(--text-caption);
		line-height: 1;
		letter-spacing: -0.01em;
		color: var(--text-secondary);
		padding: 2px 8px;
		white-space: nowrap;
		max-width: calc(100vw - 32px);
		overflow: hidden;
		text-overflow: ellipsis;
		text-shadow: 0 1px 1px rgba(0, 0, 0, 0.12);
	}
	.version-overlay.stale {
		color: var(--amber);
	}
	.seg {
		display: inline;
	}
	.dot {
		opacity: 0.55;
		margin: 0 4px;
	}
	.arrow {
		opacity: 0.7;
		margin: 0 2px;
	}
</style>
