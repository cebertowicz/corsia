<script lang="ts">
	import { get } from 'svelte/store';
	import Modal from './Modal.svelte';
	import { token } from '$lib/stores/auth';
	import { plainResponseError } from '$lib/plainError';

	let { title = 'Choose…', mode = 'dir', ext = '', startPath = null, rootPath = null, canSelect, onSelect, onClose }: {
		title?: string;
		mode?: 'dir' | 'file';
		ext?: string;
		startPath?: string | null;
		rootPath?: string | null;
		canSelect?: (path: string) => boolean;
		onSelect: (path: string) => void;
		onClose: () => void;
	} = $props();

	let cwd = $state<string | null>(null);
	let parent = $state<string | null>(null);
	let dirs = $state<{ name: string; is_git: boolean; is_link?: boolean }[]>([]);
	let files = $state<{ name: string }[]>([]);
	let loading = $state(true);
	let error = $state<string | null>(null);

	async function load(path: string | null) {
		const t = get(token);
		if (!t) return;
		loading = true;
		error = null;
		try {
			const qs = new URLSearchParams({ token: t });
			if (path) qs.set('path', path);
			if (mode === 'file') {
				qs.set('files', '1');
				if (ext) qs.set('ext', ext);
			}
			const res = await fetch(`/api/fs/browse?${qs}`);
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not browse that folder. Try again.'));
			const d = await res.json();
			cwd = d.path;
			parent = rootPath && (d.path === rootPath || !d.parent?.startsWith(rootPath)) ? null : d.parent;
			dirs = d.dirs;
			files = d.files ?? [];
		} catch (e) {
			error = e instanceof Error ? e.message : 'Browse failed';
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		load(startPath);
	});

	const selectable = $derived(!!cwd && !loading && (canSelect ? canSelect(cwd) : true));
	const shownPath = $derived(cwd ? cwd.replace(/^\/(Users|home)\/[^/]+/, '~') : '');
</script>

<Modal {title} size="sm" {onClose}>
	<div class="picker">
		<div class="crumb mono" title={cwd ?? ''}>{shownPath}</div>

		{#if error}
			<div class="err" role="alert">{error}</div>
		{:else if loading}
			<div class="empty">Loading…</div>
		{:else}
			<div class="list">
				{#if parent}
					<button class="row up press-tint" onclick={() => load(parent)}>
						<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
						<span class="nm">Up</span>
					</button>
				{/if}
				{#each dirs as d (d.name)}
					<button class="row press-tint" disabled={!!rootPath && d.is_link} onclick={() => load(`${cwd}/${d.name}`)}>
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
						<span class="nm">{d.name}</span>
						{#if d.is_git}<span class="badge">git</span>{/if}
					</button>
				{/each}
				{#each files as f (f.name)}
					<button class="row file press-tint" onclick={() => { onSelect(`${cwd}/${f.name}`); onClose(); }}>
						<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
						<span class="nm">{f.name}</span>
					</button>
				{/each}
				{#if dirs.length === 0 && files.length === 0 && !parent}
					<div class="empty">Nothing here.</div>
				{/if}
			</div>
		{/if}
	</div>

	{#snippet foot()}
		<button class="btn ghost push" onclick={onClose}>Cancel</button>
		{#if mode === 'dir'}
			<button class="btn primary" disabled={!selectable} onclick={() => { if (cwd) { onSelect(cwd); onClose(); } }}>
				Choose this folder
			</button>
		{/if}
	{/snippet}
</Modal>

<style>
	.picker { display: flex; flex-direction: column; gap: 8px; }
	.crumb {
		font-size: var(--text-footnote); color: var(--text-tertiary);
		padding: 6px 9px; border-radius: var(--radius-sm); background: var(--bg-active);
		overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
	}
	.mono { font-family: var(--font-mono, monospace); }

	.list { max-height: 300px; overflow-y: auto; display: flex; flex-direction: column; }
	.row {
		display: flex; align-items: center; gap: 9px; width: 100%; text-align: left;
		padding: 7px 9px; border-radius: var(--radius-sm); background: none; border: none;
		cursor: pointer; color: var(--text-primary); font: inherit; font-size: var(--text-subhead);
	}
	.row:hover { background: var(--bg-hover); }
	.row:disabled { opacity: 0.5; cursor: not-allowed; }
	.row svg { color: var(--text-tertiary); flex-shrink: 0; }
	.row.up { color: var(--text-secondary); }
	.nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.badge {
		font-size: var(--text-caption); font-weight: 700; font-family: var(--font-mono, monospace);
		color: var(--accent-text); border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
		padding: 0 5px; border-radius: var(--radius-pill); flex-shrink: 0;
	}

	.empty { padding: 18px 6px; text-align: center; color: var(--text-tertiary); font-size: var(--text-subhead); }
	.err {
		font-size: var(--text-subhead); color: var(--red);
		background: color-mix(in srgb, var(--red) 8%, transparent);
		border: 1px solid color-mix(in srgb, var(--red) 25%, transparent);
		border-radius: var(--radius-sm); padding: 8px 10px;
	}

	.btn {
		font: inherit; font-size: var(--text-subhead); font-weight: 500;
		padding: 8px 16px; border-radius: var(--radius-sm); cursor: pointer;
		border: 1px solid transparent;
	}
	.btn.push { margin-left: auto; }
	.btn.ghost { background: none; border-color: var(--border); color: var(--text-secondary); }
	.btn.ghost:hover { color: var(--text-primary); background: var(--bg-hover); }
	.btn.primary { background: var(--accent-fill); color: var(--accent-on-fill); }
	.btn:disabled { opacity: 0.5; cursor: not-allowed; }

</style>
