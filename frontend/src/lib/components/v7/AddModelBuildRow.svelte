<script lang="ts">
	import type { LocalModelRecommendation } from '$lib/conductorSources';
	import Button from '$lib/components/ui/Button.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import { buildQuality, type ModelGroup } from './addModelRows';

	interface Props {
		group: ModelGroup;
		build: LocalModelRecommendation;
		recommended: boolean;
		running: boolean;
		percent: number;
		failed: boolean;
		downloaded: boolean;
		busy: boolean;
		pendingAction: 'start' | 'cancel' | 'delete' | null;
		otherRunning: boolean;
		error: string;
		onDownload: () => void;
		onCancel: () => void;
		onDelete: () => void;
	}
	const { group, build, recommended, running, percent, failed, downloaded, busy, pendingAction, otherRunning, error, onDownload, onCancel, onDelete }: Props = $props();
	const buildName = $derived(build.repository.split('/').at(-1)?.match(/(oQ[\d.]+e(?:-mtp)?|mlx-[\w.]+)$/i)?.[0] ?? '');
	const size = $derived(typeof build.download_size_gb === 'number' ? `${Math.round(build.download_size_gb)} GB` : '');
	const speed = $derived(build.tokens_per_second == null ? '' : `~${Math.round(build.tokens_per_second)} tokens/s`);
	const quality = $derived(buildQuality(group, build));
	const need = $derived(build.disk?.state === 'insufficient'
		? `${build.disk.required_bytes ? `Needs ${Math.ceil(build.disk.required_bytes / 1000 ** 3)} GB of disk` : 'Needs more disk space'}${build.disk.free_bytes == null ? '' : ` · ${Math.floor(build.disk.free_bytes / 1000 ** 3)} GB free`}`
		: build.disk?.state === 'unknown' ? 'Couldn’t check free disk space' : '');
</script>

<div class="build" class:stack={failed}>
	<div class="facts">
		<div class="line">
			<span>{[size, speed, buildName].filter(Boolean).join(' · ')}</span>
			{#if build.on_disk && !failed}<span>· on this computer</span>{/if}
			{#if recommended}<span class="recommended">Recommended</span>{/if}
		</div>
		<div class="detail">{quality}{quality && build.context_window ? ' · ' : ''}{build.context_window ? `${Math.round(build.context_window / 1024)}K context` : ''}</div>
		{#if build.section && build.section !== 'best'}
			<div class="detail">oMLX: {build.section} list{build.quantization_bits ? ` · ${build.quantization_bits}-bit` : ''}{build.table_on_ssd && build.table_bytes ? ` · ${Math.round(build.table_bytes / 1000 ** 3)} GB on disk while running` : ''}</div>
		{/if}
		{#if running}<div class="progress" role="status"><Spinner /> Downloading {percent}%</div>{/if}
		{#if failed}<div class="error">{downloaded ? 'Downloaded, but couldn’t be added. Retry to finish adding it.' : 'Couldn’t download this model.'}</div>{/if}
		{#if need && !build.on_disk && !running && !failed}<div class="detail">{need}</div>{/if}
		{#if error}<div class="error" role="alert">{error}</div>{/if}
	</div>
	<div class="actions">
		{#if running}
			<Button size="sm" variant="ghost" ariaLabel={`Cancel download of ${group.name} ${buildName}`} disabled={busy} loading={pendingAction === 'cancel'} onclick={onCancel}>Cancel</Button>
		{:else if failed}
			<Button size="sm" variant="ghost" ariaLabel={`Retry download of ${group.name} ${buildName}`} disabled={busy} loading={pendingAction === 'start'} onclick={onDownload}>Retry</Button>
			<Button size="sm" variant="ghost" ariaLabel={`Delete download of ${group.name} ${buildName}`} disabled={busy} loading={pendingAction === 'delete'} onclick={onDelete}>Delete download</Button>
		{:else if !build.on_disk}
			<Button size="sm" variant="secondary" ariaLabel={`Download ${group.name} ${buildName}`} disabled={busy || otherRunning || Boolean(need)} loading={pendingAction === 'start'} onclick={onDownload}>Download</Button>
		{/if}
	</div>
</div>

<style>
	.build { display: flex; justify-content: space-between; align-items: center; gap: var(--space-3); padding: var(--space-2) 0; }
	.facts { min-width: 0; color: var(--text-secondary); font-size: var(--text-footnote); }
	.line { display: flex; flex-wrap: wrap; column-gap: var(--space-1); color: var(--text-primary); }
	.detail { color: var(--text-secondary); margin-top: var(--space-1); }
	.recommended { color: var(--accent-text); font-weight: var(--fw-semibold); }
	.progress { display: flex; align-items: center; gap: var(--space-1); color: var(--accent-text); margin-top: var(--space-1); }
	.error { color: var(--red-text); margin-top: var(--space-1); }
	.actions { display: flex; flex-wrap: wrap; gap: var(--space-1); flex-shrink: 0; }
	/* Only the two failed-state actions drop below the facts on a narrow screen; one Download stays beside them. */
	@media (max-width: 30rem) { .build.stack { align-items: flex-start; flex-wrap: wrap; } }
</style>
