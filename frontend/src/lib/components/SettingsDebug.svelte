<script lang="ts">
	import {
		myBuild,
		serverVersion,
		isFrontendStale,
		isBackendBehindOnDisk,
		loadServerVersion,
		reloadWithCacheBust,
		formatHHMM,
	} from '$lib/version';
	import { coreSettings, updateSettings } from '$lib/stores/settings';
	import { readSystemVersion, restartSystem, waitForSystemRestart } from '$lib/stores/settingsSystemRestart';
	import { Button, Toggle } from '$lib/components/ui';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsRow from './SettingsRow.svelte';

	let settingsError = $state<string | null>(null);
	let refreshing = $state(false);

	function relativeFromNow(iso: string | undefined): string {
		if (!iso) return '—';
		const t = new Date(iso).getTime();
		if (isNaN(t)) return '—';
		const diff = Date.now() - t;
		const min = Math.floor(diff / 60000);
		if (min < 1) return 'just now';
		if (min < 60) return `${min} min ago`;
		const hr = Math.floor(min / 60);
		if (hr < 24) return `${hr} hr ago`;
		const day = Math.floor(hr / 24);
		return `${day} day${day === 1 ? '' : 's'} ago`;
	}

	function formatIso(iso: string | undefined): string {
		if (!iso) return '—';
		const d = new Date(iso);
		if (isNaN(d.getTime())) return '—';
		return d.toLocaleString();
	}

	async function refresh() {
		refreshing = true;
		await loadServerVersion();
		refreshing = false;
	}

	async function setOverlay(checked: boolean) {
		const result = await updateSettings({
			features: { debug_version_overlay: checked },
		});
		if (!result.ok) settingsError = result.error ?? 'Failed to update';
	}

	async function setCpuProfiler(checked: boolean) {
		const result = await updateSettings({
			features: { cpu_profiler: checked },
		});
		if (!result.ok) settingsError = result.error ?? 'Failed to update';
	}

	const cpuProfilerOn = $derived($coreSettings?.features?.cpu_profiler === true);
	const overlayOn = $derived(
		$coreSettings?.features?.debug_version_overlay === true,
	);
	const stale = $derived($isFrontendStale);
	const backendBehind = $derived($isBackendBehindOnDisk);
	const serverFE = $derived($serverVersion?.frontend);
	const serverBE = $derived($serverVersion?.backend);
	const diskHead = $derived($serverVersion?.backend?.current_head);
	const freshLine = $derived(
		!$serverVersion ? 'Checking server…'
		: stale ? `This browser is running an older bundle (${myBuild.short_hash}) than the server has deployed (${serverFE?.short_hash}). Reload with cache-bust to pick up the new build.`
		: "Up to date — browser bundle matches the server's deployed frontend.",
	);

	let restarting = $state(false);
	async function restartBackend() {
		restarting = true;
		settingsError = null;
		try {
			const before = await readSystemVersion();
			const result = await restartSystem('restart');
			if (!result.ok) {
				settingsError = result.message ?? 'Restart could not be started.';
				return;
			}
			const completed = await waitForSystemRestart(before, result.operation_id);
			if (!completed.ok) {
				settingsError = completed.message ?? 'Restart did not finish.';
				return;
			}
			await loadServerVersion();
		} finally {
			restarting = false;
		}
	}
</script>

{#if settingsError}
	<p class="error" role="alert">{settingsError}</p>
{/if}

<SettingsGroup label="Bundle freshness" hint="Build identity for the running browser bundle, the server's deployed frontend, and the backend process.">
	<SettingsRow name="Browser bundle" sub={freshLine} subTone={stale ? 'attention' : 'quiet'} wrap>
		{#snippet actions()}
			{#if stale}<Button variant="ghost" size="sm" onclick={reloadWithCacheBust}>Reload (cache-bust)</Button>{/if}
			{#if $serverVersion}
				<Button variant="ghost" size="sm" onclick={refresh} disabled={refreshing}>{refreshing ? 'Checking…' : 'Re-check'}</Button>
			{/if}
		{/snippet}
	</SettingsRow>
</SettingsGroup>

<SettingsGroup label="On-screen overlay" hint="Shows a one-line build label at the top of every page so screenshots carry build identity. Read-only — does not affect anything else.">
	<SettingsRow
		name="Show version overlay on every page"
		sub={'Displays "b <hash> · f <hash> · HH:MM" in muted text at top-center. Turns amber when the browser bundle is stale vs. the server.'}
		wrap
	>
		{#snippet actions()}
			<Toggle checked={overlayOn} onchange={setOverlay} ariaLabel="Show version overlay on every page" />
		{/snippet}
	</SettingsRow>
</SettingsGroup>

<SettingsGroup label="CPU profiler" hint="Samples CPU use ten times a second and logs a summary each minute. Leave off unless diagnosing slowness.">
	<SettingsRow name="Profile CPU use by thread">
		{#snippet actions()}
			<Toggle checked={cpuProfilerOn} onchange={setCpuProfiler} ariaLabel="Profile CPU use by thread" />
		{/snippet}
	</SettingsRow>
</SettingsGroup>

<SettingsGroup label="Frontend — this browser bundle" hint="What the JS running right now thinks it is. Frozen at vite build time.">
	<dl class="kv">
		<dt>Build label</dt><dd><code>{myBuild.label}</code></dd>
		<dt>Short hash</dt><dd><code>{myBuild.short_hash}{myBuild.dirty === null ? '?' : myBuild.dirty ? '†' : ''}</code></dd>
		<dt>Full hash</dt><dd><code class="mono-wrap">{myBuild.full_hash}</code></dd>
		<dt>Commit</dt><dd>{myBuild.commit_subject}</dd>
		<dt>Commit time</dt><dd>{formatIso(myBuild.commit_iso)}</dd>
		<dt>Build time</dt><dd>{formatIso(myBuild.build_iso)} <span class="muted">({relativeFromNow(myBuild.build_iso)})</span></dd>
		<dt>Working tree</dt><dd>{myBuild.dirty === null ? 'unknown (git unavailable)' : myBuild.dirty ? 'dirty (uncommitted changes)' : 'clean'}</dd>
	</dl>
</SettingsGroup>

<SettingsGroup label="Frontend — server's deployed bundle" hint="What the server has on disk in dist/. If this differs from the browser bundle above, your tab is stale.">
	{#if serverFE}
		<dl class="kv">
			<dt>Build label</dt><dd><code>{serverFE.label}</code></dd>
			<dt>Short hash</dt><dd><code>{serverFE.short_hash}{serverFE.dirty === null ? '?' : serverFE.dirty ? '†' : ''}</code></dd>
			<dt>Full hash</dt><dd><code class="mono-wrap">{serverFE.full_hash}</code></dd>
			<dt>Commit</dt><dd>{serverFE.commit_subject}</dd>
			<dt>Commit time</dt><dd>{formatIso(serverFE.commit_iso)}</dd>
			<dt>Build time</dt><dd>{formatIso(serverFE.build_iso)} <span class="muted">({relativeFromNow(serverFE.build_iso)})</span></dd>
		</dl>
	{:else}
		<p class="note">Loading…</p>
	{/if}
</SettingsGroup>

<SettingsGroup label="Backend — running process" hint="The git commit the FastAPI process is running, captured at startup. Compared below against current git HEAD on disk — flags a process that hasn't been restarted since new commits landed.">
	{#if serverBE}
		{#if backendBehind}
			<SettingsRow
				name="Process"
				sub={`Running an older commit (${serverBE.short_hash}) than git HEAD on disk (${diskHead?.short_hash}). Restart to load the new code.`}
				subTone="attention"
				wrap
			>
				{#snippet actions()}
					<Button variant="ghost" size="sm" onclick={restartBackend} disabled={restarting}>{restarting ? 'Restarting…' : 'Restart backend'}</Button>
				{/snippet}
			</SettingsRow>
		{:else}
			<SettingsRow name="Process" sub="Running the latest commit on disk." />
		{/if}
		<dl class="kv">
			<dt>Build label</dt><dd><code>{serverBE.label}</code></dd>
			<dt>Short hash</dt><dd><code>{serverBE.short_hash}{serverBE.dirty === null ? '?' : serverBE.dirty ? '†' : ''}</code></dd>
			<dt>Full hash</dt><dd><code class="mono-wrap">{serverBE.full_hash}</code></dd>
			<dt>Commit</dt><dd>{serverBE.commit_subject}</dd>
			<dt>Commit time</dt><dd>{formatIso(serverBE.commit_iso)}</dd>
			<dt>Server start</dt><dd>{formatIso(serverBE.start_iso)} <span class="muted">({relativeFromNow(serverBE.start_iso)})</span></dd>
			<dt>Working tree</dt><dd>{serverBE.dirty === null ? 'unknown (git unavailable)' : serverBE.dirty ? 'dirty (uncommitted changes at start)' : 'clean'}</dd>
		</dl>
	{:else}
		<p class="note">Loading…</p>
	{/if}
</SettingsGroup>

<SettingsGroup label="Backend — current HEAD on disk" hint="What git rev-parse HEAD reports right now in the repo. Re-read every time this page loads. If this differs from the running process above, the process is stale.">
	{#if diskHead}
		<dl class="kv">
			<dt>Short hash</dt><dd><code>{diskHead.short_hash}{diskHead.dirty === null ? '?' : diskHead.dirty ? '†' : ''}</code></dd>
			<dt>Full hash</dt><dd><code class="mono-wrap">{diskHead.full_hash}</code></dd>
			<dt>Commit</dt><dd>{diskHead.commit_subject}</dd>
			<dt>Commit time</dt><dd>{formatIso(diskHead.commit_iso)}</dd>
			<dt>Working tree</dt><dd>{diskHead.dirty === null ? 'unknown (git unavailable)' : diskHead.dirty ? 'dirty (uncommitted changes right now)' : 'clean'}</dd>
		</dl>
	{:else}
		<p class="note">Loading…</p>
	{/if}
</SettingsGroup>

<style>
	.error { margin: 0 0 var(--space-4); padding: 0 var(--space-2); font-size: var(--text-footnote); color: var(--red-text); }
	.note { margin: 0; padding: 0 var(--space-2); font-size: var(--text-caption); color: var(--text-tertiary); }

	/* Read-only build facts: flat on the pane at the row inset, values in plain monospace (§0.1, §0.2). */
	.kv { display: grid; grid-template-columns: 140px minmax(0, 1fr); gap: var(--space-1) var(--space-4); margin: var(--space-1) 0 0; padding: 0 var(--space-2); font-size: var(--text-footnote); }
	.kv dt { color: var(--text-tertiary); }
	.kv dd { color: var(--text-primary); margin: 0; min-width: 0; overflow-wrap: anywhere; }
	.kv code { font-family: var(--font-mono, monospace); font-size: var(--text-footnote); }
	.kv .mono-wrap { word-break: break-all; }
	.muted { color: var(--text-tertiary); }

	@media (max-width: 768px) {
		/* One column on a phone; a long unbreakable hash breaks instead of widening the pane. */
		.kv { grid-template-columns: minmax(0, 1fr); gap: 0; }
		.kv dt { margin-top: var(--space-2); }
		.kv dt:first-child { margin-top: 0; }
	}
</style>
