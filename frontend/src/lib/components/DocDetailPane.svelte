<script lang="ts">
	import { slide } from 'svelte/transition';
	import PaneShell from './PaneShell.svelte';
	import { paneOpen, isPaneDrawer } from '$lib/stores/paneOpen';
	import { canOpenArtifact, openArtifactAndShowThread } from '$lib/openArtifactInThread';
	import { toasts } from '$lib/stores/toast';
	import {
		docLibrary, docSources, pendingCount, libraryLoading, updateDescription
	} from '$lib/stores/docLibrary';
	import { selectedDocId } from '$lib/stores/documentsSurface';

	const sel = $derived($docLibrary.find((d) => d.id === $selectedDocId) ?? null);

	let editing = $state(false);
	let saving = $state(false);
	let draft = $state('');
	function startEdit() {
		if (!sel) return;
		draft = sel.description;
		editing = true;
	}
	async function saveEdit() {
		if (!sel || saving) return;
		const t = draft.trim();
		if (!t) { editing = false; return; }
		saving = true;
		try {
			await updateDescription(sel, t);
			editing = false;
		} catch (e) {
			console.error('description save failed', e);
			toasts.show("Couldn't save the description", 'error');
		} finally {
			saving = false;
		}
	}
	$effect(() => { $selectedDocId; editing = false; }); // selection change resets edit state

	// The library is a manager, not an editing destination: opening hands the
	// document to a thread (the one that sent us here, else the active thread),
	// which is what gives its comments and edits somewhere to route.
	const openable = $derived(!!sel && canOpenArtifact());
	function openSel() {
		if (!sel || !openable) return;
		openArtifactAndShowThread({ artifactId: sel.artifactId, path: sel.abs, title: sel.name });
		if (isPaneDrawer()) paneOpen.set(false);
	}
	const srcOf = $derived(sel ? $docSources.find((s) => s.id === sel.sourceId) : null);
</script>

<PaneShell label="Documents">
	{#if sel}
		<div class="dd">
			<div class="dd-head">
				<svg class="dd-ico" class:skill={sel.skill} width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
					{#if sel.skill}<path d="M2 4h6a3 3 0 0 1 3 3v13a2.5 2.5 0 0 0-2.5-2.5H2z" /><path d="M22 4h-6a3 3 0 0 0-3 3v13a2.5 2.5 0 0 1 2.5-2.5H22z" />
					{:else}<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" />{/if}
				</svg>
				<span class="dd-name">{sel.name}</span>
				{#if sel.skill}<span class="dd-skill">skill</span>{/if}
				{#if sel.tmp}<span class="dd-tmp" title="A draft in the working folder">working</span>{/if}
			</div>
			<div class="dd-path">{srcOf?.label} · {sel.path}</div>

			<div class="dd-sec">
				<div class="dd-cap">
					Description
					{#if sel.descState === 'ai'}<span class="dd-badge ai">✦ AI</span>
					{:else if sel.descState === 'edited'}<span class="dd-badge">edited</span>{/if}
					{#if !editing}
						<button class="dd-pencil" onclick={startEdit} aria-label="Edit description" title="Edit description">
							<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /></svg>
						</button>
					{/if}
				</div>
				{#if editing}
					<div transition:slide={{ duration: 130 }}>
						<textarea class="dd-edit" rows="3" bind:value={draft} onkeydown={(e) => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') void saveEdit(); if (e.key === 'Escape') editing = false; }}></textarea>
						<div class="dd-edit-acts">
							<button class="dd-ghost" onclick={() => (editing = false)}>Cancel</button>
							<button class="dd-primary" disabled={saving} onclick={() => void saveEdit()}>{saving ? 'Saving…' : 'Save'}</button>
						</div>
					</div>
				{:else if sel.descState === 'pending'}
					<p class="dd-desc pending">No description. Use the pencil to add one.</p>
				{:else if sel.description}
					<p class="dd-desc">{sel.description}</p>
				{:else}
					<p class="dd-desc muted">No description. Use the pencil to add one, or comment on it.</p>
				{/if}
			</div>

			{#if sel.ownerThread}
				<div class="dd-sec">
					<div class="dd-cap">Owned by</div>
					<div class="dd-prov">
						<span class="dd-prov-dot"></span>
						<span class="dd-prov-name">{sel.ownerThread}</span>
					</div>
				</div>
			{/if}

			<div class="dd-stats">
				<div class="dd-stat"><span class="dd-stat-v">{sel.tokens >= 1000 ? `${(sel.tokens / 1000).toFixed(1)}k` : sel.tokens}</span><span class="dd-stat-k">tokens to read</span></div>
				<div class="dd-stat"><span class="dd-stat-v">{sel.updatedLabel || '—'}</span><span class="dd-stat-k">updated</span></div>
			</div>

			<button
				class="dd-open"
				onclick={openSel}
				disabled={!openable}
				title={openable ? undefined : 'Open a chat first — documents open beside a chat'}
			>Open document</button>
		</div>
	{:else}
		<div class="dd-empty">
			<div class="dd-cap">Sources</div>
			{#each $docSources as s (s.id)}
				<div class="dd-srcrow">
					<span class="dd-src-name">{s.label}</span>
					<span class="dd-src-meta">{s.kind === 'repo' ? 'source/ link' : s.path.replace(/^\/Users\/[^/]+/, '~')}</span>
				</div>
			{/each}
			{#if $libraryLoading}
				<p class="dd-hint">Loading files…</p>
			{:else if $pendingCount}
				<div class="dd-hygiene">{$pendingCount} file{$pendingCount > 1 ? 's' : ''} without a description. Select one to add it.</div>
			{/if}
			<p class="dd-hint">Select a file to see its description, provenance, and history.</p>
		</div>
	{/if}
</PaneShell>

<style>
	.dd { display: flex; flex-direction: column; gap: 14px; }
	.dd-head { display: flex; align-items: center; gap: 8px; }
	.dd-ico { color: var(--text-tertiary); flex-shrink: 0; }
	.dd-ico.skill { color: var(--teal); }
	.dd-name { flex: 1; min-width: 0; font-size: var(--text-body); font-weight: var(--fw-semibold); color: var(--text-primary); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.dd-skill { font-size: var(--text-caption); font-weight: var(--fw-bold); text-transform: uppercase; letter-spacing: var(--ls-cap); color: var(--teal); background: var(--teal-bg); border-radius: var(--radius-pill); padding: 2px 8px; }
	.dd-tmp { font-size: var(--text-caption); font-weight: var(--fw-bold); text-transform: uppercase; letter-spacing: var(--ls-cap); color: var(--amber); background: var(--amber-bg); border-radius: var(--radius-pill); padding: 2px 8px; }
	.dd-path { font-size: var(--text-caption); color: var(--text-tertiary); margin-top: -8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

	.dd-sec { display: flex; flex-direction: column; gap: 6px; }
	.dd-cap { display: flex; align-items: center; gap: 6px; font-size: var(--text-caption); font-weight: var(--fw-bold); text-transform: uppercase; letter-spacing: var(--ls-cap); color: var(--text-tertiary); }
	.dd-badge { font-size: var(--text-caption); font-weight: var(--fw-semibold); text-transform: none; letter-spacing: 0; color: var(--text-tertiary); background: color-mix(in srgb, var(--text-primary) 7%, transparent); border-radius: var(--radius-2xs); padding: 1px 6px; }
	.dd-badge.ai { color: var(--accent-text); background: var(--accent-wash); }
	.dd-pencil { display: flex; padding: 3px; margin-left: auto; border-radius: var(--radius-2xs); background: none; border: none; color: var(--text-tertiary); cursor: pointer; }
	.dd-pencil:hover { background: var(--bg-hover); color: var(--text-primary); }
	.dd-desc { font-size: var(--text-subhead); line-height: var(--lh-normal); color: var(--text-primary); margin: 0; }
	.dd-desc.pending { color: var(--amber); font-style: italic; }
	.dd-desc.muted { color: var(--text-tertiary); font-style: italic; }
	.dd-edit { width: 100%; box-sizing: border-box; resize: vertical; font: inherit; font-size: var(--text-footnote); padding: 8px 10px; border-radius: var(--radius-sm); border: 1px solid var(--accent); background: var(--bg-primary); color: var(--text-primary); }
	.dd-edit:focus { outline: none; }
	.dd-edit-acts { display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px; }
	.dd-ghost { font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--text-secondary); background: none; border: none; padding: 5px 8px; cursor: pointer; }
	.dd-ghost:hover { color: var(--text-primary); }
	.dd-primary { font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent-on-fill); background: var(--accent-fill); border: none; border-radius: var(--radius-xs); padding: 5px 12px; cursor: pointer; }
	.dd-primary:disabled { opacity: 0.6; cursor: default; }

	.dd-prov { display: flex; align-items: center; gap: 7px; }
	.dd-prov-dot { width: 8px; height: 8px; border-radius: var(--radius-circle); background: var(--accent); flex-shrink: 0; }
	.dd-prov-name { font-size: var(--text-subhead); color: var(--text-primary); }

	.dd-stats { display: flex; gap: 8px; }
	.dd-stat { flex: 1; display: flex; flex-direction: column; gap: 2px; padding: 9px 11px; border-radius: var(--radius-sm); background: var(--lg-card-fill, color-mix(in srgb, white 5%, transparent)); box-shadow: var(--glass-rim); }
	.dd-stat-v { font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-primary); font-family: var(--font-mono); }
	.dd-stat-k { font-size: var(--text-caption); color: var(--text-tertiary); }

	.dd-open { font-size: var(--text-footnote); font-weight: var(--fw-semibold); color: var(--accent-on-fill); background: var(--accent-fill); border: none; border-radius: var(--radius-sm); padding: 9px 14px; cursor: pointer; }
	.dd-open:hover:not(:disabled) { filter: brightness(1.06); }
	.dd-open:disabled { opacity: 0.45; cursor: default; }

	.dd-empty { display: flex; flex-direction: column; gap: 10px; }
	.dd-srcrow { display: flex; align-items: baseline; gap: 8px; padding: 4px 0; }
	.dd-src-name { font-size: var(--text-subhead); color: var(--text-primary); }
	.dd-src-meta { margin-left: auto; font-size: var(--text-caption); color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; }
	.dd-hygiene { font-size: var(--text-footnote); color: var(--amber); background: var(--amber-bg); border: 1px solid var(--amber-line); border-radius: var(--radius-sm); padding: 8px 11px; line-height: var(--lh-snug); }
	.dd-hint { font-size: var(--text-footnote); color: var(--text-tertiary); line-height: var(--lh-snug); margin: 0; }
</style>
