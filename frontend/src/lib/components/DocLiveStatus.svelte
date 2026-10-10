<script lang="ts">
	import { Button } from './ui';
	import Popover from './ui/Popover.svelte';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import { isPaneDrawer } from '$lib/stores/paneOpen';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import { reopenLiveSession } from '$lib/stores/liveDocuments';
	import { LIVE_UNAVAILABLE_NOTICE, type DocOpenRefusal, type LiveUnavailable } from '$lib/docOpenRefusal';
	import type { HeldDocument } from '$lib/stores/documentWire';
	import type { DeviceRecovery } from '$lib/stores/deviceRecovery';

	let { session = null, openRefusal = null, liveUnavailable = null, tabText = '', onReopen, onRead, connectionStatusId }: {
		session?: LiveDocumentSession | null;
		openRefusal?: DocOpenRefusal | null;
		liveUnavailable?: LiveUnavailable | null;
		tabText?: string;
		onReopen?: () => void;
		onRead?: () => void;
		connectionStatusId?: string;
	} = $props();
	type Snapshot = { connection: string; deviceSaved: boolean; deviceError: string; failureLines: string[];
		pending: boolean; held: HeldDocument | null; recovery: DeviceRecovery[]; text: string; writable: boolean; refusal: DocOpenRefusal | null };
	let view = $state.raw<Snapshot | null>(null);
	let detailsOpen = $state(false);
	let opener = $state<HTMLElement | null>(null);
	let busy = $state(false);
	let actionError = $state('');
	let copied = $state<string[]>([]);
	type Confirmation = { title: string; description: string; started: LiveDocumentSession; run: () => Promise<unknown> };
	let confirmation = $state<Confirmation | null>(null);
	$effect(() => {
		const current = session;
		detailsOpen = false; confirmation = null; actionError = ''; copied = []; busy = false;
		if (!current) { view = null; return; }
		return current.subscribe(value => {
			view = { connection: value.connection, deviceSaved: value.deviceSaved, deviceError: value.deviceError,
				failureLines: value.failureLines, pending: value.pending, held: value.held,
				recovery: value.recovery, text: value.text.toString(), writable: value.writable, refusal: value.refusal };
		});
	});
	async function copy(text: string, storeId?: string): Promise<void> {
		try {
			await navigator.clipboard.writeText(text);
			if (storeId) copied = [...copied, storeId];
		} catch { actionError = 'Could not copy the text. Keep this tab open and select it to copy.'; }
	}
	async function run(started: LiveDocumentSession, action: () => Promise<unknown>): Promise<void> {
		if (busy || session !== started) return;
		busy = true; actionError = '';
		try { await action(); }
		catch (error) { if (session === started) actionError = error instanceof Error ? error.message : String(error); }
		finally { if (session === started) busy = false; }
	}
	function confirmChoice(choice: 'corsia' | 'file'): void {
		const started = session, held = view?.held;
		if (!started || !held) return;
		confirmation = {
			started,
			title: choice === 'corsia' ? 'Keep CORSIA text, overwrite the file' : "Use the file's text, replace CORSIA text",
			description: choice === 'corsia' ? 'This replaces the outside file with the current CORSIA text.' : 'This replaces the current CORSIA text with the outside file’s text.',
			run: async () => {
				if (started.held !== held) throw new Error('The outside-change choice changed. Read the document before choosing again.');
				return started.choose(choice);
			},
		};
	}
	function confirmReplace(recovery: DeviceRecovery): void {
		const started = session;
		if (!started) return;
		confirmation = { started, title: 'Replace with my text',
			description: 'This replaces the current server document and its file with the text kept on this device.',
			run: async () => { await started.replace(recovery.text); started.dismissRecovery(recovery.storeId); } };
	}
	function accept(): void {
		const selected = confirmation;
		confirmation = null;
		if (selected) void run(selected.started, selected.run);
	}
	function closeDetails(): void { detailsOpen = false; opener?.focus(); }
	// A load refusal, or a live session refused on reopen, keeps its text; damaged-store and reasonless non-live refusals have no Retry.
	const refusal = $derived(openRefusal ?? view?.refusal ?? null);
	const hasDetails = $derived(Boolean(view?.pending || view?.held || view?.recovery.length || view?.deviceError));
</script>

{#if refusal}
	<div class="live-block" role="alert">
		<strong>{refusal.title}</strong><p>{refusal.message}</p>
		<Button size="sm" variant="ghost" onclick={() => copy(openRefusal ? tabText : view!.text)}>Copy</Button>
		{#if refusal.code !== 'store_damaged' && refusal.retryable !== false}<Button size="sm" variant="ghost" onclick={() => openRefusal ? onReopen?.() : session && reopenLiveSession(session)}>Retry</Button>{/if}
	</div>
{/if}
{#if liveUnavailable}
	<div class="live-block" role="status"><strong>{LIVE_UNAVAILABLE_NOTICE[liveUnavailable].title}</strong><p>{LIVE_UNAVAILABLE_NOTICE[liveUnavailable].message}</p></div>
{/if}
{#if view}
	{#if view.connection === 'disconnected'}
		<div class="live-line" id={connectionStatusId} role="status">
			<span>{view.deviceSaved ? 'Disconnected · changes stay on this device' : "Disconnected · changes aren't saved; keep this tab open"}</span>
			{#if !view.deviceSaved}<Button size="sm" variant="ghost" onclick={() => copy(view!.text)}>Copy text</Button>{/if}
		</div>
	{:else if view.connection === 'reconnecting'}<div class="live-line" id={connectionStatusId} role="status">Reconnecting…</div>{/if}
	{#if view.connection === 'connected' && view.deviceError}
		<div class="live-block" role="status">Changes aren't saved on this device; keep this tab open. <Button size="sm" variant="ghost" onclick={() => copy(view!.text)}>Copy text</Button></div>
	{/if}
	{#if view.pending}<div class="live-block" role="status">This write is still settling. The file may already have changed; saving on the server is not confirmed.</div>{/if}
	{#if view.held}<div class="live-block" role="status">This file changed outside CORSIA while a write was being saved.</div>{/if}
	{#if view.recovery.length}<div class="live-block" role="status">This document was reset on the server. Your unsaved text is on this device.</div>{/if}
	{#if view.failureLines.length}
		<div class="live-block" role="status">
			{#each view.failureLines.slice(0, 2) as line}<span>{line}</span>{/each}
			{#if view.failureLines.length > 2}<span>+{view.failureLines.length - 2} more</span>{/if}
			<Button size="sm" variant="ghost" disabled={!view.writable || view.pending || Boolean(view.held)} loading={busy} onclick={() => session && run(session, () => session!.retry())}>Retry</Button>
			<Button size="sm" variant="ghost" onclick={() => copy(view!.text)}>Copy text</Button>
		</div>
	{/if}
	{#if hasDetails}<button type="button" class="live-details" aria-expanded={detailsOpen} onclick={(event) => { opener = event.currentTarget; detailsOpen = true; }}>Details</button>{/if}
{/if}
{#if actionError}<div class="live-block" role="alert">{actionError}</div>{/if}

{#snippet details()}
	{#if view?.pending}
		<p>Read the document before writing again.</p>
		<Button size="sm" variant="ghost" onclick={() => copy(view!.text)}>Copy text</Button>
		<Button size="sm" variant="ghost" onclick={onRead}>Reload</Button>
	{/if}
	{#if view?.deviceError}<p>{view.deviceError}</p>{/if}
	{#if view?.held}
		{#each [{ label: 'CORSIA text', text: view.held.corsia_text }, { label: 'file text', text: view.held.file_text }, { label: 'interrupted text', text: view.held.candidate_text }] as item (item.label)}
			<section><h3>{item.label}</h3><pre>{item.text}</pre><Button size="sm" variant="ghost" onclick={() => copy(item.text)}>Copy {item.label}</Button></section>
		{/each}
		<Button size="sm" variant="ghost" disabled={busy || !view.writable || view.held.choice_pending} onclick={() => confirmChoice('corsia')}>Keep CORSIA text, overwrite the file</Button>
		<Button size="sm" variant="ghost" disabled={busy || !view.writable || view.held.choice_pending} onclick={() => confirmChoice('file')}>Use the file's text, replace CORSIA text</Button>
	{/if}
	{#each view?.recovery ?? [] as recovery (recovery.storeId)}
		<section><h3>Text kept on this device</h3><pre>{recovery.text}</pre>
			<Button size="sm" variant="ghost" onclick={() => copy(recovery.text, recovery.storeId)}>Copy text</Button>
			<Button size="sm" variant="ghost" disabled={busy || !view?.writable} onclick={() => confirmReplace(recovery)}>Replace with my text</Button>
			{#if copied.includes(recovery.storeId)}<Button size="sm" variant="ghost" onclick={() => session?.dismissRecovery(recovery.storeId)}>Dismiss</Button>{/if}
		</section>
	{/each}
	<Button size="sm" variant="ghost" onclick={closeDetails}>Close</Button>
{/snippet}
{#if detailsOpen}
	{#if isPaneDrawer()}
		<AdaptiveDrawer ariaLabel="Document recovery" heading="Document recovery" onClose={closeDetails} initialFocusSelector="button">{@render details()}</AdaptiveDrawer>
	{:else}
		<Popover anchor={opener} open={detailsOpen} onClose={closeDetails} restoreFocus={() => opener?.focus()} placement="bottom">{@render details()}</Popover>
	{/if}
{/if}
<ConfirmDialog open={Boolean(confirmation)} title={confirmation?.title ?? ''} description={confirmation?.description ?? ''} confirmLabel={confirmation?.title} onConfirm={accept} onCancel={() => confirmation = null} />

<style>
	.live-line { display: flex; align-items: center; gap: 8px; color: var(--text-secondary); font-size: var(--text-caption); white-space: nowrap; }
	.live-block { padding: 8px 0; color: var(--text-primary); font-size: var(--text-subhead); line-height: 1.5; }
	.live-block p { margin: 4px 0; }
	section { margin-block: 12px; }
	h3 { font-size: var(--text-subhead); margin: 0 0 6px; }
	.live-details { border: 0; background: none; padding: 0; color: var(--text-secondary); font: inherit; font-size: var(--text-caption); text-decoration: underline; cursor: pointer; }
	pre { white-space: pre-wrap; overflow-wrap: anywhere; font-family: inherit; font-size: var(--text-caption); }
</style>
