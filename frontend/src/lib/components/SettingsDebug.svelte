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
		$coreSettings?.features?.debug_version_overlay !== false,
	);
	const stale = $derived($isFrontendStale);
	const backendBehind = $derived($isBackendBehindOnDisk);
	const serverFE = $derived($serverVersion?.frontend);
	const serverBE = $derived($serverVersion?.backend);
	const diskHead = $derived($serverVersion?.backend?.current_head);

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

<div class="settings-section">
	<div class="section-header">
		<h2>Debug</h2>
		<p>Build identity for the running browser bundle, the server's deployed frontend, and the backend process.</p>
	</div>

	{#if settingsError}
		<div class="settings-error">{settingsError}</div>
	{/if}

	<div class="settings-card">
		<h3>Bundle freshness</h3>
		{#if !$serverVersion}
			<p class="card-desc status-pending">Checking server…</p>
		{:else if stale}
			<p class="card-desc status-stale">
				⚠ This browser is running an older bundle ({myBuild.short_hash})
				than the server has deployed ({serverFE?.short_hash}).
				Reload with cache-bust to pick up the new build.
			</p>
			<div class="card-actions">
				<button class="btn primary" onclick={reloadWithCacheBust}>Reload (cache-bust)</button>
				<button class="btn sm ghost" onclick={refresh} disabled={refreshing}>
					{refreshing ? 'Checking…' : 'Re-check'}
				</button>
			</div>
		{:else}
			<p class="card-desc status-ok">
				✓ Up to date — browser bundle matches the server's deployed frontend.
			</p>
			<div class="card-actions">
				<button class="btn sm ghost" onclick={refresh} disabled={refreshing}>
					{refreshing ? 'Checking…' : 'Re-check'}
				</button>
			</div>
		{/if}
	</div>

	<div class="settings-card">
		<h3>On-screen overlay</h3>
		<p class="card-desc">Shows a one-line build label at the top of every page so screenshots carry build identity. Read-only — does not affect anything else.</p>
		<div class="feature-row">
			<div class="feature-info">
				<span class="feature-name">Show version overlay on every page</span>
				<span class="feature-desc">Displays "b &lt;hash&gt; · f &lt;hash&gt; · HH:MM" in muted text at top-center. Turns amber when the browser bundle is stale vs. the server.</span>
			</div>
			<label class="toggle-switch" title="Toggle version overlay">
				<input
					type="checkbox"
					checked={overlayOn}
					onchange={(e: Event) => setOverlay((e.target as HTMLInputElement).checked)}
				/>
				<span class="toggle-track"></span>
			</label>
		</div>
	</div>

	<div class="settings-card">
		<h3>CPU profiler</h3>
		<p class="card-desc">Samples CPU use ten times a second and logs a summary each minute. Leave off unless diagnosing slowness.</p>
		<div class="feature-row">
			<div class="feature-info">
				<span class="feature-name">Profile CPU use by thread</span>
			</div>
			<label class="toggle-switch" title="Toggle CPU profiler">
				<input
					type="checkbox"
					checked={cpuProfilerOn}
					onchange={(e: Event) => setCpuProfiler((e.target as HTMLInputElement).checked)}
				/>
				<span class="toggle-track"></span>
			</label>
		</div>
	</div>

	<div class="settings-card">
		<h3>Frontend — this browser bundle</h3>
		<p class="card-desc">What the JS running right now thinks it is. Frozen at <code>vite build</code> time.</p>
		<dl class="kv">
			<dt>Build label</dt><dd><code>{myBuild.label}</code></dd>
			<dt>Short hash</dt><dd><code>{myBuild.short_hash}{myBuild.dirty === null ? '?' : myBuild.dirty ? '†' : ''}</code></dd>
			<dt>Full hash</dt><dd><code class="mono-wrap">{myBuild.full_hash}</code></dd>
			<dt>Commit</dt><dd>{myBuild.commit_subject}</dd>
			<dt>Commit time</dt><dd>{formatIso(myBuild.commit_iso)}</dd>
			<dt>Build time</dt><dd>{formatIso(myBuild.build_iso)} <span class="muted">({relativeFromNow(myBuild.build_iso)})</span></dd>
			<dt>Working tree</dt><dd>{myBuild.dirty === null ? 'unknown (git unavailable)' : myBuild.dirty ? 'dirty (uncommitted changes)' : 'clean'}</dd>
		</dl>
	</div>

	<div class="settings-card">
		<h3>Frontend — server's deployed bundle</h3>
		<p class="card-desc">What the server has on disk in <code>dist/</code>. If this differs from the browser bundle above, your tab is stale.</p>
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
			<p class="card-desc">Loading…</p>
		{/if}
	</div>

	<div class="settings-card">
		<h3>Backend — running process</h3>
		<p class="card-desc">The git commit the FastAPI process is running, captured at startup. Compared below against current git HEAD on disk — flags a process that hasn't been restarted since new commits landed.</p>
		{#if serverBE}
			{#if backendBehind}
				<p class="card-desc status-stale">
					⚠ Running an older commit ({serverBE.short_hash}) than git HEAD on disk ({diskHead?.short_hash}). Restart to load the new code.
				</p>
				<div class="card-actions" style="margin-bottom: 16px;">
					<button class="btn primary" onclick={restartBackend} disabled={restarting}>
						{restarting ? 'Restarting…' : 'Restart backend'}
					</button>
				</div>
			{:else}
				<p class="card-desc status-ok">✓ Running the latest commit on disk.</p>
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
			<p class="card-desc">Loading…</p>
		{/if}
	</div>

	<div class="settings-card">
		<h3>Backend — current HEAD on disk</h3>
		<p class="card-desc">What <code>git rev-parse HEAD</code> reports right now in the repo. Re-read every time this page loads. If this differs from the running process above, the process is stale.</p>
		{#if diskHead}
			<dl class="kv">
				<dt>Short hash</dt><dd><code>{diskHead.short_hash}{diskHead.dirty === null ? '?' : diskHead.dirty ? '†' : ''}</code></dd>
				<dt>Full hash</dt><dd><code class="mono-wrap">{diskHead.full_hash}</code></dd>
				<dt>Commit</dt><dd>{diskHead.commit_subject}</dd>
				<dt>Commit time</dt><dd>{formatIso(diskHead.commit_iso)}</dd>
				<dt>Working tree</dt><dd>{diskHead.dirty === null ? 'unknown (git unavailable)' : diskHead.dirty ? 'dirty (uncommitted changes right now)' : 'clean'}</dd>
			</dl>
		{:else}
			<p class="card-desc">Loading…</p>
		{/if}
	</div>
</div>

<style>
	.settings-section { padding: 24px 32px; max-width: 760px; }
	.section-header { margin-bottom: 24px; }
	.section-header h2 { font-size: var(--text-title2); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.section-header p { font-size: var(--text-subhead); color: var(--text-muted); margin: 0; }

	.settings-card { background: var(--bg-secondary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 20px 24px; margin-bottom: 16px; }
	.settings-card h3 { font-size: var(--text-callout); font-weight: 600; color: var(--text-primary); margin: 0 0 4px; }
	.card-desc { font-size: var(--text-subhead); color: var(--text-muted); margin: 0 0 16px; line-height: 1.5; }
	.settings-error { background: color-mix(in srgb, var(--red) 10%, transparent); border: 1px solid color-mix(in srgb, var(--red) 30%, transparent); color: var(--red); border-radius: var(--radius-xs); padding: 10px 14px; font-size: var(--text-subhead); margin-bottom: 16px; }

	.status-ok { color: var(--text-secondary); }
	.status-stale { color: var(--amber); }
	.status-pending { color: var(--text-muted); }

	.card-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.btn { display: inline-flex; align-items: center; gap: 5px; padding: 7px 14px; border-radius: var(--radius-xs); font-size: var(--text-subhead); font-weight: 500; font-family: inherit; cursor: pointer; border: 1px solid var(--border-subtle); background: var(--bg-primary); color: var(--text-primary); transition: background var(--dur-tight); }
	.btn:hover { background: var(--bg-hover); }
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.btn.sm { padding: 4px 10px; font-size: var(--text-footnote); }
	.btn.ghost { background: none; }
	.btn.primary { background: var(--accent); border-color: var(--accent); color: white; }
	.btn.primary:hover { filter: brightness(1.06); }

	.feature-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--border-subtle); }
	.feature-row:last-child { border-bottom: none; }
	.feature-info { display: flex; flex-direction: column; gap: 3px; flex: 1; }
	.feature-name { font-size: var(--text-subhead); font-weight: 500; color: var(--text-primary); }
	.feature-desc { font-size: var(--text-footnote); color: var(--text-muted); line-height: 1.5; }

	.toggle-switch { position: relative; display: inline-flex; align-items: center; cursor: pointer; flex-shrink: 0; margin-top: 2px; }
	.toggle-switch input { position: absolute; opacity: 0; width: 0; height: 0; }
	.toggle-track { width: 36px; height: 20px; background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); transition: background var(--dur-standard), border-color var(--dur-standard); position: relative; }
	.toggle-track::after { content: ''; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; background: var(--text-muted); border-radius: var(--radius-circle); transition: transform var(--dur-standard), background var(--dur-standard); }
	.toggle-switch input:checked + .toggle-track { background: var(--accent); border-color: var(--accent); }
	.toggle-switch input:checked + .toggle-track::after { transform: translateX(16px); background: white; }

	.kv { display: grid; grid-template-columns: 140px 1fr; gap: 8px 16px; margin: 0; font-size: var(--text-subhead); }
	.kv dt { color: var(--text-muted); font-weight: 500; }
	.kv dd { color: var(--text-primary); margin: 0; }
	.kv code { font-family: 'SF Mono', ui-monospace, Menlo, monospace; font-size: var(--text-footnote); background: var(--bg-primary); border: 1px solid var(--border-subtle); border-radius: var(--radius-2xs); padding: 1px 6px; }
	.kv .mono-wrap { word-break: break-all; }
	.muted { color: var(--text-tertiary); font-size: var(--text-footnote); }

	@media (max-width: 768px) {
		.settings-section { padding: 16px; }
		/* A `1fr` track is `minmax(auto, 1fr)`, so one unbreakable 40-character commit
		   hash in a value — a CI merge subject, or any full hash — widens the whole
		   card and the settings pane starts scrolling sideways on a phone. Zero the
		   track's minimum and let values break instead of pushing the page. */
		.kv { grid-template-columns: minmax(0, 1fr); gap: 2px 0; }
		.kv dt { margin-top: 8px; }
		.kv dd { min-width: 0; overflow-wrap: anywhere; }
	}
</style>
