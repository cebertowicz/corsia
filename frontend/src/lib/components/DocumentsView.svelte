<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { fade, slide } from 'svelte/transition';
	import { cubicOut } from 'svelte/easing';
	import { canOpenArtifact, openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import { uiScope } from '$lib/stores/uiScope';
	import { token } from '$lib/stores/auth';
	import { contextAttachTarget, type ContextAttachTarget } from '$lib/stores/contextAttach';
	import { threads } from '$lib/stores/threads';
	import { loadSettings } from '$lib/stores/settings';
	import { toasts } from '$lib/stores/toast';
	import { on } from '$lib/ws/events';
	import { linkRepo } from '$lib/api/docs';
	import {
		docLibrary, docSources, docActivity, skillDocs, pendingCount,
		libraryLoading, libraryError, loadLibrary,
		dirDescription, secondaryDocumentText, type DocEntry
	} from '$lib/stores/docLibrary';
	import { docLens, selectedDocId, type DocLens } from '$lib/stores/documentsSurface';
	import LinkRepoModal from './LinkRepoModal.svelte';
	import { plainResponseError } from '$lib/plainError';
	import { StatePill } from '$lib/components/ui';

	const lens = $derived($docLens);
	const entries = $derived($docLibrary);
	const sources = $derived($docSources);
	let search = $state('');

	const CHIPS: { id: DocLens; label: string }[] = [
		{ id: 'all', label: 'All' },
		{ id: 'skills', label: 'Skills' },
		{ id: 'recent', label: 'Recent' },
		{ id: 'activity', label: 'Activity' }
	];

	let reloadT: ReturnType<typeof setTimeout> | null = null;
	function scheduleReload() {
		if (reloadT) clearTimeout(reloadT);
		reloadT = setTimeout(() => void loadLibrary(), 400);
	}
	$effect(() => {
		$uiScope?.id;
		void loadLibrary();
	});
	onMount(() => {
		const off = on('artifacts_changed', scheduleReload);
		return () => {
			off?.();
			if (reloadT) clearTimeout(reloadT);
		};
	});

	const q = $derived(search.trim().toLowerCase());
	const searched = $derived(q ? entries.filter((e) => e.searchKey.includes(q)) : null);
	const attachTarget = $derived($contextAttachTarget);
	let attachingPath = $state<string | null>(null);
	let addOpen = $state(false);
	let sheetOpen = $state(false);
	let repoPath = $state('~/');
	$effect(() => {
		if (attachTarget && addOpen) addOpen = false;
	});

	interface DirGroup { dir: string; files: DocEntry[] }
	const groupsBySource = $derived.by(() => {
		const bySrc = new Map<string, Map<string, DocEntry[]>>();
		for (const e of entries) {
			const dir = e.path.includes('/') ? e.path.slice(0, e.path.lastIndexOf('/')) : '';
			let byDir = bySrc.get(e.sourceId);
			if (!byDir) bySrc.set(e.sourceId, (byDir = new Map()));
			let files = byDir.get(dir);
			if (!files) byDir.set(dir, (files = []));
			files.push(e);
		}
		const out = new Map<string, DirGroup[]>();
		for (const [src, byDir] of bySrc) {
			out.set(src, [...byDir.entries()]
				.sort((a, b) => a[0].localeCompare(b[0]))
				.map(([dir, files]) => ({ dir, files: files.sort((a, b) => a.name.localeCompare(b.name)) })));
		}
		return out;
	});
	function groupsFor(sourceId: string): DirGroup[] {
		return groupsBySource.get(sourceId) ?? [];
	}
	let collapsed = $state<Set<string>>(new Set());
	function toggleDir(key: string) {
		const next = new Set(collapsed);
		next.has(key) ? next.delete(key) : next.add(key);
		collapsed = next;
	}

	const recent = $derived(entries.slice().sort((a, b) => b.updatedTs - a.updatedTs));

	function select(e: DocEntry) {
		selectedDocId.set(e.id);
		if (isPaneDrawer()) paneOpen.set(true);
	}
	// Opening from the library hands the document to a thread; with no thread to
	// own it, this stays a selection so the detail pane can explain why.
	function openEntry(e: DocEntry) {
		selectedDocId.set(e.id);
		if (!canOpenArtifact()) {
			if (isPaneDrawer()) paneOpen.set(true);
			return;
		}
		openArtifactAndShowThread({ artifactId: e.artifactId, path: e.abs, title: e.name });
		if (isPaneDrawer()) paneOpen.set(false);
	}

	function updateThreadFiles(threadId: string, files: string[]) {
		threads.update((rows) =>
			rows.map((thread) => (thread.id === threadId ? { ...thread, bootloader_files: files } : thread)),
		);
	}

	function attachModeLabel(): string {
		if (!attachTarget) return '';
		const target = attachTarget.targetLabel?.trim();
		if (attachTarget.scope === 'workspace') return target ? `Add to ${target}` : 'Add to workspace context';
		if (attachTarget.scope === 'project') return target ? `Add to project context: ${target}` : 'Add to project context';
		return target ? `Add to thread context: ${target}` : 'Add to thread context';
	}

	function attachAriaLabel(e: DocEntry): string {
		if (!attachTarget) return '';
		if (attachTarget.scope === 'workspace') return `Add ${e.name} to workspace context`;
		if (attachTarget.scope === 'project') return `Add ${e.name} to project context`;
		return `Add ${e.name} to thread context`;
	}

	function isContextFile(e: DocEntry): boolean {
		const lower = e.name.toLowerCase();
		return lower.endsWith('.md') || lower.endsWith('.markdown') || lower.endsWith('.txt');
	}

	function normalizePath(value: string): string {
		return value.replace(/\\/g, '/').replace(/^\.\//, '').replace(/\/$/, '');
	}

	function existingContextMatch(existing: string, e: DocEntry): boolean {
		const saved = normalizePath(existing);
		if (!saved) return false;
		const abs = normalizePath(e.abs);
		const rel = normalizePath(e.path);
		const name = normalizePath(e.name);
		return saved === abs || saved === rel || saved === name || abs.endsWith(`/${saved}`);
	}

	function isAlreadyAttached(e: DocEntry): boolean {
		return (attachTarget?.existingFiles ?? []).some((file) => existingContextMatch(file, e));
	}

	function canAttachContextEntry(e: DocEntry): boolean {
		return Boolean(attachTarget) && isContextFile(e) && !isAlreadyAttached(e);
	}

	function attachRequestBody(target: ContextAttachTarget, e: DocEntry) {
		if (target.scope === 'workspace') return { scope: 'workspace', path: e.abs };
		if (target.scope === 'project') {
			if (!target.projectId) throw new Error('Project context target is missing');
			return { scope: 'project', project_id: target.projectId, path: e.abs };
		}
		if (!target.threadId) throw new Error('Thread context target is missing');
		return { scope: 'thread', thread_id: target.threadId, path: e.abs };
	}

	async function attachContextEntry(e: DocEntry) {
		const target = get(contextAttachTarget);
		if (!target || attachingPath || !canAttachContextEntry(e)) return;
		const t = get(token);
		if (!t) {
			toasts.show('Sign in required', 'error');
			return;
		}
		attachingPath = e.abs;
		try {
			const body = attachRequestBody(target, e);
			const res = await fetch('/api/context/bootloader/attach', {
				method: 'POST',
				headers: {
					Authorization: `Bearer ${t}`,
					'Content-Type': 'application/json',
				},
				body: JSON.stringify(body),
			});
			if (!res.ok) throw new Error(await plainResponseError(res, 'Could not load your documents. Try again.'));
			const result = await res.json();
			if (target.scope === 'thread' && target.threadId) {
				updateThreadFiles(target.threadId, result.files ?? [e.abs]);
			} else if (target.scope === 'workspace') {
				await loadSettings();
			}
			const nextFiles = Array.isArray(result.files)
				? result.files
				: [...(target.existingFiles ?? []), e.abs];
			contextAttachTarget.update((current) => {
				if (!current) return current;
				if (
					current.scope !== target.scope ||
					current.threadId !== target.threadId ||
					current.projectId !== target.projectId
				) return current;
				return { ...current, existingFiles: nextFiles };
			});
			toasts.show('Added to context', 'success');
		} catch (err) {
			console.error('attach context file failed', err);
			toasts.show(err instanceof Error ? err.message : "Couldn't add context file", 'error');
		} finally {
			attachingPath = null;
		}
	}

	async function mountRepo() {
		const pid = get(uiScope)?.id;
		if (!pid) return;
		sheetOpen = false; addOpen = false;
		try {
			await linkRepo(pid, repoPath.trim());
			await loadLibrary();
			toasts.show('Directory linked into source/', 'success');
		} catch (e) {
			console.error('link repo failed', e);
			toasts.show(e instanceof Error ? e.message : 'Link failed', 'error');
		}
	}

	const ACT_COLOR: Record<string, string> = {
		added: 'var(--green)', described: 'var(--purple)'
	};
</script>

<div class="docs" in:fade={{ duration: 160, easing: cubicOut }}>
	<div class="bar">
		<div class="search">
			<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" /></svg>
			<input placeholder="Search docs…" bind:value={search} />
			{#if search}<button class="clr" onclick={() => (search = '')} aria-label="Clear">✕</button>{/if}
		</div>
		{#if !attachTarget && $uiScope}
		<div class="addwrap">
			<button class="add" class:open={addOpen} onclick={() => (addOpen = !addOpen)}>＋ Add</button>
			{#if addOpen}
				<div class="add-menu" transition:slide={{ duration: 120 }}>
					<button class="add-item press-tint" onclick={() => { sheetOpen = true; addOpen = false; }}>Link repo directory…</button>
				</div>
			{/if}
		</div>
		{/if}
	</div>

	{#if attachTarget}
		<div class="attach-mode" role="status">
			<span>{attachModeLabel()}</span>
			<button type="button" onclick={() => contextAttachTarget.set(null)}>Cancel</button>
		</div>
	{/if}

	<div class="chips">
		{#each CHIPS as c (c.id)}
			<button class="chip" class:on={lens === c.id} onclick={() => docLens.set(c.id)}>
				{c.label}
				{#if c.id === 'skills'}<span class="chip-n">{$skillDocs.length}</span>{/if}
				{#if c.id === 'all' && $pendingCount}<span class="chip-dot" title="{$pendingCount} file{$pendingCount > 1 ? 's' : ''} without a description"></span>{/if}
			</button>
		{/each}
	</div>

	<div class="body">
		{#if $libraryError}
			<div class="lib-error">
				<span>Couldn't load the library.</span>
				<code>{$libraryError}</code>
				<button onclick={() => void loadLibrary()}>Retry</button>
			</div>
		{:else if $libraryLoading && entries.length === 0}
			{#each [92, 78, 85, 70, 80, 64] as w, i (i)}<div class="row-skel" style="width:{w}%"></div>{/each}
		{:else if searched}
			{#each searched as e (e.id)}{@render row(e, true)}{/each}
			{#if searched.length === 0}<p class="empty">No docs match “{search}”.</p>{/if}

		{:else if lens === 'activity'}
			<p class="lens-note">Recent file activity.</p>
			<div class="feed">
				{#each $docActivity as a (a.id)}
					<div class="act-row">
						<span class="act-dot" style="background:{ACT_COLOR[a.kind]}"></span>
						<div class="act-main">
							<span class="act-text">{a.text}</span>
							<span class="act-meta">{a.actor} · {a.at}</span>
						</div>
					</div>
				{/each}
			</div>

		{:else if lens === 'skills'}
			<p class="lens-note">Skills and colocated files are documents — edit them here; agents load SKILL.md definitions as behavior.</p>
			{#each $skillDocs as e (e.id)}{@render row(e, true)}{/each}

		{:else if lens === 'recent'}
			{#each recent.slice(0, 10) as e (e.id)}{@render row(e, true)}{/each}

		{:else}
			{#each sources as s (s.id)}
				<div class="src">
					<div class="src-head">
						{#if s.kind === 'repo'}
							<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3 0 6-2 6-5.5.08-1.25-.27-2.48-1-3.5.28-1.15.28-2.35 0-3.5 0 0-1 0-3 1.5-2.64-.5-5.36-.5-8 0C6 2 5 2 5 2c-.3 1.15-.3 2.35 0 3.5A5.403 5.403 0 0 0 4 9c0 3.5 3 5.5 6 5.5-.39.49-.68 1.05-.85 1.65-.17.6-.22 1.23-.15 1.85v4" /><path d="M9 18c-4.51 2-5-2-7-2" /></svg>
						{:else}
							<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" /></svg>
						{/if}
						<span class="src-name">{s.label}</span>
						{#if s.capped}<span class="src-sync" title="Listing capped — narrow with search">capped</span>{/if}
					</div>
					{#each groupsFor(s.id) as g (g.dir)}
						{@const key = `${s.id}:${g.dir}`}
						{#if g.dir !== ''}
							<button class="dir press-tint" onclick={() => toggleDir(key)}>
								<svg class="dir-chev" class:open={!collapsed.has(key)} width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18l6-6-6-6" /></svg>
								<span class="dir-name">{g.dir}/</span>
								<span class="dir-desc">{dirDescription(g.dir)}</span>
								<span class="dir-n">{g.files.length}</span>
							</button>
						{/if}
						{#if g.dir === '' || !collapsed.has(key)}
							{#each g.files as e (e.id)}{@render row(e, false)}{/each}
						{/if}
					{/each}
				</div>
			{/each}
		{/if}
	</div>

	<LinkRepoModal
		open={sheetOpen}
		path={repoPath}
		onPathChange={(next) => (repoPath = next)}
		onSubmit={mountRepo}
		onCancel={() => (sheetOpen = false)}
	/>
</div>

{#snippet row(e: DocEntry, showPath: boolean)}
	<div class="row press-tint" class:sel={$selectedDocId === e.id} role="button" tabindex="0"
		onclick={() => select(e)} ondblclick={() => openEntry(e)}
		onkeydown={(ev) => { if (ev.key === 'Enter') openEntry(e); }}>
		<svg class="row-ico" class:skill={e.skill} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
			{#if e.skill}<path d="M2 4h6a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H2z" /><path d="M22 4h-6a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H22z" />
			{:else}<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" />{/if}
		</svg>
		<div class="row-main">
			<span class="row-name">{showPath ? e.path : e.name}</span>
			{#if e.descState === 'pending' || e.description || e.ownerName}
				<span class="row-desc" class:pending={e.descState === 'pending'}>{secondaryDocumentText(e)}</span>
			{/if}
		</div>
		{#if e.freshnessState === 'overdue'}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		<span class="row-meta">{e.updatedLabel}</span>
		{#if attachTarget}
			{#if isAlreadyAttached(e)}
				<button
					class="row-attach added"
					aria-label="{e.name} is already in context"
					disabled
				>Added</button>
			{:else if isContextFile(e)}
				<button
					class="row-attach"
					title={attachModeLabel()}
					aria-label={attachAriaLabel(e)}
					disabled={attachingPath !== null}
					onclick={(ev) => { ev.stopPropagation(); void attachContextEntry(e); }}
				>{attachingPath === e.abs ? 'Adding' : 'Add'}</button>
			{/if}
		{:else}
			<button class="row-open" title="Open" aria-label="Open {e.name}" onclick={(ev) => { ev.stopPropagation(); openEntry(e); }}>
				<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17L17 7M9 7h8v8" /></svg>
			</button>
		{/if}
	</div>
{/snippet}

<style>
	.docs { width: 100%; max-width: 860px; margin: 0 auto; display: flex; flex-direction: column; gap: 10px; text-align: left; padding: 4px 0 24px; }

	.bar { display: flex; align-items: center; gap: 8px; }
	.search { flex: 1; display: flex; align-items: center; gap: 8px; padding: 7px 11px; border-radius: var(--radius-sm); background: var(--bg-secondary); border: 1px solid var(--border-subtle); color: var(--text-tertiary); }
	.search input { flex: 1; min-width: 0; background: none; border: none; outline: none; font: inherit; font-size: var(--text-subhead); color: var(--text-primary); }
	.clr { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-size: var(--text-footnote); padding: 0 2px; }
	.addwrap { position: relative; }
	.add { font-size: var(--text-footnote); font-weight: var(--fw-medium); color: var(--text-secondary); background: none; border: 1px dashed var(--border); border-radius: var(--radius-sm); padding: 7px 12px; cursor: pointer; white-space: nowrap; }
	.add:hover, .add.open { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
	.add-menu { position: absolute; top: calc(100% + 6px); right: 0; z-index: 30; min-width: 210px; padding: 5px; display: flex; flex-direction: column; gap: 1px; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--glass-rim), var(--shadow-chip); }
	.add-item { text-align: left; padding: 8px 10px; border-radius: var(--radius-sm); background: none; border: none; cursor: pointer; font-size: var(--text-subhead); color: var(--text-primary); }
	.add-item:hover { background: var(--bg-hover); }

	.chips { display: flex; align-items: center; gap: 6px; }
	.chip { display: inline-flex; align-items: center; gap: 6px; font-size: var(--text-footnote); font-weight: var(--fw-medium); color: var(--text-secondary); background: none; border: 1px solid var(--border-subtle); border-radius: var(--radius-pill); padding: 4px 12px; cursor: pointer; transition: color var(--dur-tight), background var(--dur-tight), border-color var(--dur-tight); }
	.chip:hover { color: var(--text-primary); }
	.chip.on { color: var(--accent-text); background: var(--accent-wash); border-color: color-mix(in srgb, var(--accent) 35%, transparent); }
	.chip-n { font-family: var(--font-mono); font-size: var(--text-caption); color: var(--text-tertiary); }
	.chip-dot { width: 6px; height: 6px; border-radius: var(--radius-circle); background: var(--amber); }

	.body { display: flex; flex-direction: column; gap: 2px; }
	.attach-mode {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		min-height: 34px;
		padding: 6px 10px;
		border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
		border-radius: var(--radius-sm);
		background: var(--accent-wash);
		color: var(--text-primary);
		font-size: var(--text-footnote);
	}
	.attach-mode span {
		font-weight: var(--fw-semibold);
	}
	.attach-mode button {
		border: 0;
		border-radius: var(--radius-xs);
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
		font: inherit;
		font-size: var(--text-caption);
		padding: 3px 6px;
	}
	.attach-mode button:hover {
		background: var(--bg-hover);
		color: var(--text-primary);
	}
	.empty { font-size: var(--text-footnote); color: var(--text-tertiary); padding: 18px 4px; }
	.lens-note { font-size: var(--text-footnote); color: var(--text-tertiary); margin: 2px 2px 8px; }

	.src { margin-bottom: 10px; }
	.src-head { display: flex; align-items: center; gap: 7px; padding: 10px 4px 6px; color: var(--text-tertiary); }
	.src-name { font-size: var(--text-caption); font-weight: var(--fw-bold); text-transform: uppercase; letter-spacing: var(--ls-cap); color: var(--text-secondary); }
	.src-sync { font-size: var(--text-caption); color: var(--text-tertiary); margin-left: auto; }

	.dir { display: flex; align-items: center; gap: 7px; width: 100%; text-align: left; padding: 6px 6px; border-radius: var(--radius-sm); background: none; border: none; cursor: pointer; }
	.dir:hover { background: var(--lg-row-hover, var(--bg-hover)); }
	.dir-chev { color: var(--text-tertiary); transition: transform var(--dur-tight); transform: rotate(0deg); flex-shrink: 0; }
	.dir-chev.open { transform: rotate(90deg); }
	.dir-name { font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-primary); font-family: var(--font-mono); }
	.dir-desc { flex: 1; min-width: 0; font-size: var(--text-footnote); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.dir-n { font-family: var(--font-mono); font-size: var(--text-caption); color: var(--text-tertiary); }

	.row { display: flex; align-items: center; gap: 9px; padding: 7px 6px 7px 24px; border-radius: var(--radius-sm); cursor: pointer; transition: background var(--dur-tight); }
	.row:hover { background: var(--lg-row-hover, var(--bg-hover)); }
	.row.sel { background: var(--accent-wash); box-shadow: inset 2px 0 0 var(--accent); }
	.row-ico { flex-shrink: 0; color: var(--text-tertiary); }
	.row-ico.skill { color: var(--teal); }
	.row-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
	.row-name { font-size: var(--text-subhead); color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.row-desc { font-size: var(--text-footnote); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.row-desc.pending { color: var(--amber); font-style: italic; }
	.row-meta { flex-shrink: 0; font-size: var(--text-caption); color: var(--text-tertiary); }
	.row-open { flex-shrink: 0; display: flex; padding: 5px; border-radius: var(--radius-xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; opacity: 0; transition: opacity var(--dur-tight), background var(--dur-tight), color var(--dur-tight); }
	.row:hover .row-open, .row.sel .row-open { opacity: 1; }
	.row-open:hover { background: var(--bg-hover); color: var(--accent-text); }
	.row-attach {
		flex-shrink: 0;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		min-width: 36px;
		min-height: 32px;
		border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
		border-radius: var(--radius-xs);
		background: transparent;
		color: var(--accent-text);
		cursor: pointer;
		font: inherit;
		font-size: var(--text-caption);
		font-weight: var(--fw-semibold);
		padding: 4px 9px;
		transition: opacity var(--dur-tight), background var(--dur-tight), border-color var(--dur-tight), color var(--dur-tight);
		opacity: 1;
		pointer-events: auto;
	}
	.row-attach:hover:not(:disabled) { background: var(--accent-wash); }
	.row-attach:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}
	.row-attach.added {
		border-color: var(--border-subtle);
		color: var(--text-tertiary);
		opacity: 1;
	}

	.lib-error { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; padding: 14px; border-radius: var(--radius-sm); color: var(--red); background: color-mix(in srgb, var(--red) 8%, transparent); border: 1px solid color-mix(in srgb, var(--red) 25%, transparent); font-size: var(--text-footnote); }
	.lib-error code { font-size: var(--text-caption); color: var(--text-tertiary); word-break: break-all; }
	.lib-error button { font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--red); background: none; border: 1px solid currentColor; border-radius: var(--radius-xs); padding: 3px 10px; cursor: pointer; }
	.row-skel { height: 38px; border-radius: var(--radius-sm); background: var(--bg-hover); margin: 3px 0; animation: pulse 1.4s ease-in-out infinite; }

	.feed { display: flex; flex-direction: column; gap: 2px; }
	.act-row { display: flex; align-items: flex-start; gap: 10px; padding: 9px 6px; border-radius: var(--radius-sm); }
	.act-row:hover { background: var(--lg-row-hover, var(--bg-hover)); }
	.act-dot { flex-shrink: 0; width: 8px; height: 8px; border-radius: var(--radius-circle); margin-top: 5px; }
	.act-main { display: flex; flex-direction: column; gap: 1px; }
	.act-text { font-size: var(--text-subhead); color: var(--text-primary); }
	.act-meta { font-size: var(--text-caption); color: var(--text-tertiary); }


	@media (max-width: 768px) {
		.search input { font-size: var(--text-input, 16px); }
		.row-open { opacity: 1; }
		.row-attach {
			min-width: 44px;
			min-height: 44px;
			padding: 0 var(--space-3);
		}
	}

</style>
