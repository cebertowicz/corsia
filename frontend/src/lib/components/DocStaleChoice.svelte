<script lang="ts">
	import { Button } from './ui';
	import Popover from './ui/Popover.svelte';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import { isPaneDrawer } from '$lib/stores/paneOpen';
	import { fetchContent, type DocContent } from '$lib/api/docs';
	import { viewerContent, type ViewerContent } from '$lib/docViewerLoading';
	import { docSaveDrafts, keepMyText, useFileText } from '$lib/stores/docSaveQueue';

	/**
	 * After a save was refused because the file changed after this tab's base: both texts, and the person's choice of which
	 * to keep. It has the held live save's structure (DocLiveStatus): a Details control, both versions with Copy, the two
	 * choices behind a confirmation. The editor's text is only ever replaced through `onUseFile`, the viewer's own content writer.
	 */
	let { tabKey, onUseFile }: { tabKey: string; onUseFile: (content: ViewerContent) => void } = $props();

	type Read = { state: 'loading' } | { state: 'failed' } | { state: 'ready'; file: DocContent };
	const draft = $derived($docSaveDrafts[tabKey]);
	let read = $state.raw<Read>({ state: 'loading' });
	let detailsOpen = $state(false);
	let opener = $state<HTMLElement | null>(null);
	let busy = $state(false);
	let notice = $state('');
	let actionError = $state('');
	let confirmation = $state<'mine' | 'file' | null>(null);
	let readId = 0;

	async function load(): Promise<void> {
		const target = draft?.target;
		if (!target) return;
		const id = ++readId;
		read = { state: 'loading' };
		try {
			const file = await fetchContent(target.path, { projectId: target.projectId, threadId: target.threadId });
			if (id === readId) read = { state: 'ready', file };
		} catch {
			if (id === readId) read = { state: 'failed' };
		}
	}
	function openDetails(event: MouseEvent): void {
		opener = event.currentTarget as HTMLElement;
		notice = ''; actionError = '';
		detailsOpen = true;
		void load();
	}
	function closeDetails(): void { detailsOpen = false; readId++; opener?.focus(); }
	async function copy(text: string): Promise<void> {
		try { await navigator.clipboard.writeText(text); }
		catch { actionError = 'Couldn’t copy the text. Select it and copy it instead.'; }
	}
	async function accept(): Promise<void> {
		const choice = confirmation, target = draft?.target;
		confirmation = null;
		if (!choice || !target || read.state !== 'ready' || busy) return;
		const file = read.file;
		if (choice === 'file') {
			useFileText(target, { text: file.text, revision: file.revision ?? null });
			onUseFile(viewerContent(file, target.artifactId));
			return;
		}
		busy = true; actionError = '';
		try {
			if (await keepMyText(target, file.text)) return;
			// A file that changed again refuses again: the text stays, and Details shows the file as it is now.
			if ($docSaveDrafts[tabKey]?.stale) { notice = 'This document changed again. Choose which text to keep.'; await load(); }
			else actionError = $docSaveDrafts[tabKey]?.error || 'Couldn’t save the document. Your text stays here. Choose Keep your text to try again.';
		} finally { busy = false; }
	}
	const CONFIRMS = {
		mine: { title: 'Keep your text', description: 'This replaces the file’s text with yours.' },
		file: { title: 'Use the file’s text', description: 'This replaces your text with the file’s text.' },
	};
</script>

<button type="button" class="stale-details" aria-expanded={detailsOpen} onclick={openDetails}>Details</button>

{#snippet details()}
	{#if notice}<p role="status">{notice}</p>{/if}
	{#if read.state === 'loading'}
		<p role="status">Loading the file’s text…</p>
	{:else if read.state === 'failed'}
		<p role="alert">Couldn’t load the file’s text. Your text stays here.</p>
		<Button size="sm" variant="ghost" onclick={load}>Retry</Button>
	{:else}
		{#each [{ label: 'Your text', text: draft?.editText ?? '', copyLabel: 'Copy your text' }, { label: 'The file’s text', text: read.file.text, copyLabel: 'Copy file text' }] as item (item.label)}
			<section><h3>{item.label}</h3><pre>{item.text}</pre><Button size="sm" variant="ghost" onclick={() => copy(item.text)}>{item.copyLabel}</Button></section>
		{/each}
		<Button size="sm" variant="ghost" disabled={busy} onclick={() => confirmation = 'mine'}>Keep your text</Button>
		<Button size="sm" variant="ghost" disabled={busy} onclick={() => confirmation = 'file'}>Use the file’s text</Button>
	{/if}
	{#if actionError}<p role="alert">{actionError}</p>{/if}
	<Button size="sm" variant="ghost" onclick={closeDetails}>Close</Button>
{/snippet}
{#if detailsOpen}
	{#if isPaneDrawer()}
		<AdaptiveDrawer ariaLabel="Choose which text to keep" heading="Choose which text to keep" onClose={closeDetails} initialFocusSelector="button">{@render details()}</AdaptiveDrawer>
	{:else}
		<Popover anchor={opener} open={detailsOpen} onClose={closeDetails} restoreFocus={() => opener?.focus()} placement="bottom" ariaLabel="Choose which text to keep">{@render details()}</Popover>
	{/if}
{/if}
<ConfirmDialog open={confirmation !== null} title={confirmation ? CONFIRMS[confirmation].title : ''} description={confirmation ? CONFIRMS[confirmation].description : ''}
	confirmLabel={confirmation ? CONFIRMS[confirmation].title : ''} destructive={confirmation === 'file'} onConfirm={accept} onCancel={() => confirmation = null} />

<style>
	.stale-details { border: 0; background: none; padding: 0; margin-left: var(--space-2); color: inherit; font: inherit; text-decoration: underline; cursor: pointer; }
	section { margin-block: var(--space-3); }
	h3 { font-size: var(--text-subhead); margin: 0 0 var(--space-2); }
	pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: var(--text-caption); max-height: 30vh; overflow: auto; }
</style>
