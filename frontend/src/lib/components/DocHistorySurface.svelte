<script lang="ts">
	// The history drawer shares its layout with the document comments drawer.
	import { onDestroy, tick, untrack } from 'svelte';
	import { Button, IconButton } from '$lib/components/ui';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import ToolDiff from './ToolDiff.svelte';
	import { lineDiff } from '$lib/diff';
	import { formatMoment } from '$lib/time';
	import { HISTORY_BESIDE_CANVAS, contentWidth } from '$lib/imageViewerPane';
	import { fetchHistory, fetchVersionText, type DocVersion } from '$lib/docHistory';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import { connectionState } from '$lib/stores/connection';
	import { RequestRefused } from '$lib/api/docs';
	import { emitProductEvent } from '$lib/product_events';

	let { target, narrow, within = null, covered = false, onClose, applyRestore, onRestored, restoring = $bindable(false) }: {
		target: DocViewerTarget;
		narrow: boolean;
		within?: HTMLElement | null;
		covered?: boolean;
		onClose: () => void;
		/** Asks the server to restore; it owns the restore, so a refusal or a lost answer is all this surface sees. */
		applyRestore: (version: DocVersion) => Promise<void>;
		/** Shows the document's current text, as an agent's write to it would. */
		onRestored: () => Promise<void>;
		/** A restore is pending; the viewer binds it to its history controller, which owns it, so the save line reads "Saving…". */
		restoring?: boolean;
	} = $props();

	// Each refusal kind's line and the one action that can help: sending the restore again, reloading the list, or none.
	const REFUSALS: Record<string, { line: string; action: 'restore' | 'reload' | null }> = {
		failed: { line: 'Couldn’t restore this version. The document is unchanged.', action: 'restore' },
		stale: { line: 'Couldn’t restore this version. The document changed since you opened it.', action: 'restore' },
		unsaved_typing: { line: 'Couldn’t restore this version. Your latest typing isn’t saved yet, so nothing was restored.', action: 'restore' },
		not_loaded: { line: 'Couldn’t restore this version. The document is still loading.', action: 'restore' },
		unconfirmed: { line: 'Couldn’t confirm the restore. The newest version doesn’t hold this version’s text.', action: 'restore' },
		history_off: { line: 'Couldn’t restore this version. This document’s history is off, so copy any text you need from the changes below.', action: null },
		version_unavailable: { line: 'Couldn’t restore this version. This document’s history doesn’t include it. Reload history to see the versions it has.', action: 'reload' },
		invalid_skill: { line: 'Couldn’t restore this version. Its text isn’t a valid skill. Copy any text you need from the changes below.', action: null },
		context_read_only: { line: 'Couldn’t restore this version. This file is read-only. Copy any text you need from the changes below.', action: null },
	};

	let load = $state<'loading' | 'error' | 'ready'>('loading');
	let offline = $state(false);
	let loadFailure = $state('Couldn’t load history. Try again.');
	let versionFailure = $state('Couldn’t load history. Try again.');
	let versions = $state<DocVersion[]>([]);
	let truncated = $state(false);
	let texts = $state<Record<string, string>>({});
	let versionLoad = $state<'loading' | 'error' | 'ready'>('loading');
	let versionRetry = $state(0);
	let open = $state<number | null>(null);
	let detent = $state<'full' | 'half'>('full');
	let wide = $state(false);
	let diffScroll = $state<HTMLElement>();
	let listEl = $state<HTMLElement>();
	let backEl = $state<HTMLButtonElement>();
	let refusal = $state<string | null>(null);
	// The answer to a restore never arrived; the reloaded list decides whether it landed (settleLost).
	let lost = $state(false);
	let pressHead: string | undefined;
	let pressed: DocVersion | undefined;
	let alive = true;
	onDestroy(() => { alive = false; if (restoring) restoring = false; });

	$effect(() => {
		if ($connectionState !== 'connected') { offline = true; return; }
		if (untrack(() => offline || load === 'loading')) void retry();
	});

	// The container query that moves the list beside the text is on the canvas; the markup follows the same line (HISTORY_BESIDE_CANVAS).
	$effect(() => {
		if (narrow || !within) { wide = false; return; }
		const measure = (width: number) => { wide = width >= HISTORY_BESIDE_CANVAS; };
		measure(contentWidth(within).width);
		const watcher = new ResizeObserver((entries) => measure(entries[0].contentBoxSize?.[0]?.inlineSize ?? contentWidth(within).width));
		watcher.observe(within);
		return () => watcher.disconnect();
	});

	const when = (version: DocVersion) => formatMoment(new Date(version.savedAt), new Date(), { clock: true });
	const absolute = (version: DocVersion) => formatMoment(new Date(version.savedAt), new Date(), { absolute: true });
	const shown = $derived(open === null ? null : versions[open] ?? null);
	const isCurrent = $derived(open === 0);
	const changes = $derived.by(() => {
		if (!shown || open === null) return undefined;
		const before = versions[open + 1];
		if (!(shown.id in texts) || (before && !(before.id in texts))) return undefined;
		return lineDiff(before ? texts[before.id] : '', texts[shown.id]);
	});
	// Why Restore is unavailable, said in its accessible name; it stays focusable so the reason can be reached.
	const restoreBlock = $derived(offline ? 'needs a connection to CORSIA' : isCurrent ? 'this is the current version' : '');

	$effect(() => {
		versionRetry;
		const version = shown;
		const before = open === null ? undefined : versions[open + 1];
		const cached = untrack(() => texts);
		if (!version) return;
		let active = true;
		versionLoad = 'loading';
		void Promise.all([version, ...(before ? [before] : [])].map(async (row) => {
			if (cached[row.id] === undefined) return [row.id, await fetchVersionText(target, row.id)] as const;
			return [row.id, cached[row.id]] as const;
		})).then((loaded) => {
			if (!active) return;
			texts = { ...texts, ...Object.fromEntries(loaded) };
			versionLoad = 'ready';
		}).catch((error) => { if (active) { versionFailure = historyFailure(error); versionLoad = 'error'; } });
		return () => { active = false; };
	});

	// Every version opens scrolled so its first change is in view.
	$effect(() => {
		const key = shown?.id;
		const box = diffScroll;
		if (!key || !box || versionLoad !== 'ready') return;
		void tick().then(() => {
			const first = box.querySelector<HTMLElement>('.diff-line.add, .diff-line.del');
			if (!first) { box.scrollTop = 0; return; }
			const offset = first.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
			box.scrollTop = Math.max(0, offset - box.clientHeight / 3);
		});
	});

	function showList(): void {
		open = null;
		void tick().then(() => { listEl?.scrollTo({ top: 0 }); listEl?.querySelector<HTMLElement>('.vrow')?.focus(); });
	}

	function historyFailure(error: unknown): string {
		return error instanceof TypeError ? 'Couldn’t load history. Check the connection and try again.' : 'Couldn’t load history. Try again.';
	}
	async function retry(): Promise<void> {
		try {
			const selected = shown?.id;
			const refreshed = await fetchHistory(target);
			if (!alive) return;
			versions = refreshed.versions;
			truncated = refreshed.truncated;
			if (selected && open !== null) {
				const index = versions.findIndex((row) => row.id === selected);
				open = index < 0 ? null : index;
			}
			offline = false;
			load = 'ready';
			versionRetry++;
			if (lost) {
				lost = false;
				await settleLost();
			}
		} catch (error) {
			if (!alive) return;
			loadFailure = historyFailure(error);
			if (versions.length && error instanceof TypeError) { offline = true; load = 'ready'; } else load = 'error';
		}
	}
	// A lost restore landed only if a version newer than the one seen at press time holds the pressed version's text: any other write
	// (an agent's edit, or the typing saved just before the restore) adds a row too.
	async function settleLost(): Promise<void> {
		const newest = versions[0];
		if (!newest || !pressed || newest.id === pressHead) { restoring = false; refusal = 'failed'; return; }
		let landed = false;
		try { landed = (texts[newest.id] ?? await fetchVersionText(target, newest.id)) === texts[pressed.id]; } catch { landed = false; }
		if (!alive) return;
		if (landed) await restored();
		else { restoring = false; refusal = 'unconfirmed'; }
	}
	async function restored(): Promise<void> {
		emitProductEvent('document', 'version_restored');
		try { await onRestored(); } catch { /* The list still shows the recorded restore. */ }
		restoring = false;
		showList();
	}
	async function restore(): Promise<void> {
		if (!shown || restoreBlock || restoring || versionLoad !== 'ready') return;
		pressHead = versions[0]?.id;
		pressed = shown;
		restoring = true;
		refusal = null;
		try {
			await applyRestore(shown);
		} catch (error) {
			// No answer: whether it landed is unknown, so the restore is never sent again from here (design step 7). Typing that
			// couldn't be saved first is refused by the controller before anything is sent, so it never lands here.
			if (error instanceof TypeError) { lost = true; offline = true; return; }
			restoring = false;
			refusal = error instanceof RequestRefused && error.kind && error.kind in REFUSALS ? error.kind : 'failed';
			if (refusal === 'stale') {
				try { await onRestored(); } catch { /* The line still says the document changed. */ }
				await retry();
			}
			return;
		}
		await retry();
		await restored();
	}
	function reload(): void {
		refusal = null;
		void retry();
	}
	function openAt(index: number): void {
		refusal = null;
		open = index;
		emitProductEvent('document', 'version_viewed');
		if (!wide) void tick().then(() => backEl?.focus());
	}
	function back(): void {
		const was = open;
		refusal = null;
		open = null;
		void tick().then(() => { if (was !== null) listEl?.querySelectorAll<HTMLElement>('.vrow')[was]?.focus(); });
	}
	function step(by: number): void {
		if (open === null) return;
		refusal = null;
		open = open + by;
		emitProductEvent('document', 'version_viewed');
	}
	function onListKeys(event: KeyboardEvent): void {
		if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
		const rows = Array.from(listEl?.querySelectorAll<HTMLElement>('.vrow') ?? []);
		const at = rows.indexOf(document.activeElement as HTMLElement);
		if (at < 0) return;
		const next = Math.min(rows.length - 1, Math.max(0, at + (event.key === 'ArrowDown' ? 1 : -1)));
		event.preventDefault();
		rows[next].focus();
		if (wide && open !== null && open !== next) {
			open = next;
			emitProductEvent('document', 'version_viewed');
		}
	}
	function onKeys(event: KeyboardEvent): void {
		if (event.key !== 'Escape') return;
		event.stopPropagation();
		if (open !== null) back(); else onClose();
	}
	// On the sheet the drawer's focus trap takes Escape on its panel first and closes the whole sheet (focusTrap.ts:30-38, wired AdaptiveDrawer.svelte:179), so
	// the window handler above never runs there. This handler sits inside the panel and listens in the capture phase (a Svelte onkeydown is delegated to the
	// document, after the trap): with a version open, Escape goes Back to the list.
	function onSheetKeys(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || open === null) return;
		event.preventDefault();
		event.stopPropagation();
		back();
	}
</script>

{#snippet closeButton()}
	<IconButton ariaLabel="Close history" variant="ghost" size="sm" onclick={onClose}><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18" /></svg></IconButton>
{/snippet}

{#snippet restoreButton()}
	<Button size="sm" variant="ghost" ariaDisabled={restoreBlock !== '' && !restoring} disabled={restoring} ariaLabel={restoreBlock && !restoring ? `Restore: ${restoreBlock}` : undefined} onclick={restore}>{restoring ? 'Restoring…' : 'Restore'}</Button>
{/snippet}

{#snippet versionMeta(version: DocVersion)}
	<div class="version-meta">
		<div class="line1"><span class="who">{version.author}</span>{#if isCurrent}<span class="current">Current</span>{/if}<span class="when">{when(version)}</span></div>
		<div class="why full-reason">{version.reason ?? 'No reason given'}</div>
	</div>
{/snippet}

{#snippet versionDiff()}
	{#if offline}<div class="status" role="status">Showing the last loaded history. Restore needs a connection to CORSIA.<Button size="sm" onclick={retry}>Retry</Button></div>
	{:else if refusal}<div class="status" role="status">{REFUSALS[refusal].line}{#if REFUSALS[refusal].action === 'restore'}<Button size="sm" onclick={restore}>Retry</Button>{:else if REFUSALS[refusal].action === 'reload'}<Button size="sm" onclick={reload}>Reload history</Button>{/if}</div>{/if}
	<div class="scroll" bind:this={diffScroll}>
		{#if versionLoad === 'loading'}<p class="status">Loading history…</p>
		{:else if versionLoad === 'error'}<div class="status" role="status">{versionFailure}<Button size="sm" onclick={retry}>Retry</Button></div>
		{:else if changes}<ToolDiff file="" lines={changes} words fill />
		{:else}<p class="status">Changes are too long to show.</p>{/if}
	</div>
{/snippet}

{#snippet listBody()}
	<div class="heading"><h2 tabindex="-1">History {#if load === 'ready'}<span>{versions.length}</span>{/if}</h2>{@render closeButton()}</div>
	{#if load === 'loading'}<p class="status">Loading history…</p>{/if}
	{#if load === 'error'}<div class="status" role="status">{loadFailure}<Button size="sm" onclick={retry}>Retry</Button></div>{/if}
	{#if offline}<div class="status" role="status">Showing the last loaded history.{#if !(wide && open !== null)}<Button size="sm" onclick={retry}>Retry</Button>{/if}</div>{/if}
	{#if load === 'ready'}
		{#if truncated}<p class="status">Showing the {versions.length} most recent versions.</p>{/if}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="scroll" bind:this={listEl} onkeydown={onListKeys}>
			{#each versions as version, index (version.id)}
				<button type="button" class="vrow" class:open={open === index} aria-current={open === index ? 'true' : undefined} onclick={() => openAt(index)}>
					<span class="line1"><span class="who">{version.author}</span>{#if index === 0}<span class="current">Current</span>{/if}<span class="when">{when(version)}</span></span>
					<span class="why">{version.reason ?? 'No reason given'}</span>
				</button>
			{/each}
			{#if versions.length < 2}<p class="status">No earlier versions.</p>{/if}
		</div>
	{/if}
{/snippet}

{#snippet narrowBody()}
	{#if shown && open !== null}
		<div class="vhead" data-history-head aria-busy={restoring}>
			<button type="button" class="drill-back press-tint" bind:this={backEl} onclick={back}>
				<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6" /></svg>
				<span>Back</span>
			</button>
			<span class="spacer"></span>
			<Button size="sm" variant="ghost" disabled={open >= versions.length - 1} onclick={() => step(1)}>Older</Button>
			<Button size="sm" variant="ghost" disabled={open === 0} onclick={() => step(-1)}>Newer</Button>
			<span class="apart"></span>
			{@render restoreButton()}
			{@render closeButton()}
		</div>
		{@render versionMeta(shown)}
		{@render versionDiff()}
	{:else}
		{@render listBody()}
	{/if}
{/snippet}

<svelte:window onkeydown={onKeys} />
{#if narrow}
	<AdaptiveDrawer ariaLabel="Document history" headless fill opaque {detent} onDetent={(next) => detent = next} {within} {covered} open={true} {onClose} onClosed={() => {}}>
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<section class="canvas-history-drawer sheet" data-doc-history-drawer aria-label="Document history" onkeydowncapture={onSheetKeys}>{@render narrowBody()}</section>
	</AdaptiveDrawer>
{:else if wide}
	{#if shown}
		<section class="history-version" data-doc-history-version aria-label="Version of {absolute(shown)}">
			<div class="vhead-wide" aria-busy={restoring}>
				{@render versionMeta(shown)}
				{@render restoreButton()}
			</div>
			{@render versionDiff()}
		</section>
	{/if}
	<section class="canvas-history-drawer" data-doc-history-drawer aria-label="Document history">{@render listBody()}</section>
{:else}
	<section class="canvas-history-drawer" class:full={open !== null} data-doc-history-drawer aria-label="Document history">{@render narrowBody()}</section>
{/if}

<style>
	.canvas-history-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; padding: var(--space-1) var(--space-2) 0; border-top: 1px solid var(--border-subtle); }
	.canvas-history-drawer.sheet { flex: 1; padding: 0 var(--space-2); border-top: 0; }
	.history-version { grid-area: editor; display: flex; flex-direction: column; gap: var(--space-1); min-height: 0; min-width: 0; animation: history-in var(--dur-tight) ease-out; }
	@keyframes history-in { from { opacity: 0; } to { opacity: 1; } }
	.vhead-wide { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-2); flex: none; }
	.vhead-wide .version-meta { flex: 1; min-width: 0; padding-left: 0; }
	.heading { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); padding: 0 0 var(--space-1) var(--space-1); }
	.heading h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.heading h2:focus { outline: none; }
	.heading h2 span { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; margin-left: calc(var(--space-1) / 2); }
	.vhead { display: flex; align-items: center; gap: var(--space-1); flex: none; }
	.spacer { flex: 1; }
	.apart { flex: none; width: var(--space-3); }
	/* The canonical in-pane back control (PlanDrillView.svelte:90-93, same markup and rules). */
	.drill-back {
		display: inline-flex; align-items: center; gap: var(--space-1); align-self: flex-start;
		border: 0; background: transparent; color: var(--text-secondary);
		font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer;
		padding: var(--space-1) var(--space-2); border-radius: var(--radius-xs);
	}
	@media (hover: hover) { .drill-back:hover { background: var(--bg-hover); color: var(--text-primary); } }
	.drill-back:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	/* The one-row bar has no slack at 375: "Restoring…" is wider than "Restore", so Older and Newer give the width up (down to their 44 px target), never Back or its chevron. The pending button is the same Button, disabled and without its spinner (the spinner's 18 px would push Close off the row); the busy state is the label and the region's aria-busy. */
	.vhead .drill-back, .vhead .drill-back svg { flex: none; }
	.scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding-bottom: var(--space-2); }
	.vrow { position: relative; display: flex; flex-direction: column; gap: calc(var(--space-1) / 2); width: 100%; min-height: var(--list-row-min-height); padding: var(--space-1) var(--space-2); border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-primary); text-align: left; cursor: pointer; font: inherit; }
	.vrow:hover { background: var(--bg-hover); }
	/* The open row reads as the selected row does in the chat list (SidebarThreadRow.svelte:258-267). */
	.vrow.open { background: var(--accent-wash); }
	.vrow.open::before { content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: calc(var(--space-1) / 2); background: var(--accent); border-radius: var(--radius-sm) 0 0 var(--radius-sm); }
	.vrow:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	.line1 { display: flex; align-items: center; gap: var(--space-2); min-width: 0; }
	.who { font-size: var(--text-subhead); color: var(--text-primary); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	/* Status as coloured text, never a pill (DESIGN-SYSTEM §0.1 Status; the pattern of PlanRowFace.svelte:100-102 and SettingsKeys.svelte:360). */
	.current { flex: none; font-size: var(--text-caption); font-weight: var(--fw-medium); color: var(--green-text); white-space: nowrap; }
	.when { flex: none; margin-left: auto; font-size: var(--text-caption); color: var(--text-tertiary); white-space: nowrap; }
	/* A row's reason or receipt wraps to two lines and ends in an ellipsis after that, so the receipt's time is never cut mid-line. */
	.why { font-size: var(--text-caption); color: var(--text-tertiary); overflow: hidden; overflow-wrap: anywhere; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; }
	/* The open version states its reason in full: the row shows two lines, the version it opens is where the whole reason is read. */
	.why.full-reason { display: block; overflow: visible; -webkit-line-clamp: unset; line-clamp: unset; }
	.version-meta { flex: none; padding: 0 var(--space-2); display: flex; flex-direction: column; gap: calc(var(--space-1) / 2); min-width: 0; }
	.status { margin: 0; padding: var(--space-1) var(--space-2); font-size: var(--text-footnote); color: var(--text-secondary); display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-2); }
</style>
