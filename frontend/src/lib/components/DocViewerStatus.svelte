<script lang="ts">
	import { tick } from 'svelte';
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
	import DocReviewControls from './DocReviewControls.svelte';
	import { Button, StatePill } from '$lib/components/ui';
	import { formatFreshness, type DocumentFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import type { CommentFeedback } from '$lib/api/docs';

	let {
		title = 'Document',
		presence,
		reviewBar,
		commentsOpen = false,
		commentsLoading = false,
		ready,
		stale,
		readonly,
		commentCount,
		orphanedCount,
		artifactId,
		targetInstanceId,
		viewingThreadId = null,
		reviewTargetKey = null,
		onReload,
		onReloadPlacements,
		placementReloading = false,
		placementReloadError = false,
		liveConnection = null,
		onOpenComments,
		freshness,
		onFeedbackChanged,
		onMarkVerified,
		liveSession = null,
		openRefusal = null,
		liveUnavailable = null,
		tabText = '',
		onReopen,
		onRead,
	}: {
		title?: string;
		presence?: import('svelte').Snippet;
		/** The review bar sits first in the header, so its buttons come before the document in tab order. */
		reviewBar?: import('svelte').Snippet;
		commentsOpen?: boolean;
		commentsLoading?: boolean;
		ready: boolean;
		stale: boolean;
		readonly: boolean;
		commentCount: number;
		orphanedCount: number;
		artifactId: string | null;
		targetInstanceId: string | null;
		viewingThreadId?: string | null;
		reviewTargetKey?: string | null;
		onReload?: () => void;
		onReloadPlacements?: () => boolean;
		placementReloading?: boolean;
		placementReloadError?: boolean;
		liveConnection?: LiveDocumentSession['connection'] | null;
		onOpenComments: (opener: HTMLElement) => void;
		freshness: DocumentFreshness | null;
		onFeedbackChanged?: () => void;
		onMarkVerified?: () => Promise<void>;
		liveSession?: LiveDocumentSession | null;
		openRefusal?: DocOpenRefusal | null;
		liveUnavailable?: LiveUnavailable | null;
		tabText?: string;
		onReopen?: () => void;
		onRead?: () => void;
	} = $props();

	let feedbackStatus = $state<DocFeedbackStatus>();
	let detailsOpen = $state(false);
	let verifying = $state(false);
	let verificationError = $state('');
	let verificationNotice = $state('');
	let placementAnnouncement = $state('');
	let placementReloadRequested = $state(false);
	let placementReloadSeenBusy = $state(false);
	let commentStatus = $state<HTMLButtonElement>();
	const uid = $props.id();
	const unplacedId = `unplaced-${uid}`;
	// A live region only announces a change, so the stale state needs text that appears when it starts.
	const staleAnnouncement = $derived(stale && ready ? 'File changed since you opened it. Reload to see the latest.' : '');
	const freshnessPresentation = $derived(freshness ? formatFreshness(freshness) : null);
	const placementReloadVisible = $derived(Boolean(liveSession && orphanedCount && onReloadPlacements));
	const placementReloadUnavailable = $derived(liveConnection === 'disconnected' || liveConnection === 'reconnecting');
	const liveConnectionDescriptionId = $derived(liveSession ? `live-connection-${liveSession.artifactId}` : undefined);
	$effect(() => {
		artifactId;
		detailsOpen = false;
		verificationError = '';
		verificationNotice = '';
		placementAnnouncement = '';
		placementReloadRequested = false;
		placementReloadSeenBusy = false;
	});
	$effect(() => {
		if (!placementReloadRequested) return;
		if (placementReloading) { placementReloadSeenBusy = true; return; }
		if (!placementReloadSeenBusy) return;
		placementReloadRequested = false;
		placementReloadSeenBusy = false;
		placementAnnouncement = placementReloadError ? "Couldn't reload comment placements" : orphanedCount ? `${placedPhrase(orphanedCount)}` : 'Comments placed';
		if (!orphanedCount) void tick().then(() => commentStatus?.focus());
	});
	const placedPhrase = (count: number) => `${count} comment${count === 1 ? '' : 's'} still couldn't be placed`;
	const unplacedPhrase = $derived(`${orphanedCount} comment${orphanedCount === 1 ? '' : 's'} couldn't be placed`);
	function reloadPlacements(): void {
		if (!placementReloadVisible || placementReloadUnavailable || placementReloading) return;
		placementReloadRequested = onReloadPlacements?.() ?? false;
	}

	async function markVerified(): Promise<void> {
		if (!onMarkVerified || verifying) return;
		verifying = true;
		verificationError = '';
		try {
			await onMarkVerified();
			verificationNotice = 'Document marked verified.';
		} catch (error) {
			verificationError = error instanceof Error ? error.message : 'Could not mark this document verified.';
		} finally {
			verifying = false;
		}
	}

	function detailsKeys(element: HTMLElement) {
		const onKey = (event: KeyboardEvent) => {
			if (event.key !== 'Escape' || !detailsOpen || !(event.target instanceof Node)) return;
			const toggle = element.querySelector<HTMLButtonElement>('.identity-details');
			if (event.target !== toggle && !element.querySelector('.document-metadata')?.contains(event.target)) return;
			event.preventDefault();
			event.stopPropagation();
			detailsOpen = false;
			toggle?.focus();
		};
		element.addEventListener('keydown', onKey);
		return { destroy() { element.removeEventListener('keydown', onKey); } };
	}

	export function commentCreated(): void { feedbackStatus?.commentCreated(); }
	export function sync(id: string, feedback: CommentFeedback): void { feedbackStatus?.sync(id, feedback); }
</script>

<header class="document-header" use:detailsKeys>
	{#if reviewBar}{@render reviewBar()}{/if}
	<div class="document-identity">
		<button type="button" class="identity-details" aria-label="Document details" aria-expanded={detailsOpen}
			onclick={() => detailsOpen = !detailsOpen}>
			<span class="document-title" title={title}>{title}</span>
			{#if freshness && freshnessPresentation}<span class="identity-age" title={`Owner: ${freshness.owner_name} · ${freshnessPresentation.phrase}`}><span class="identity-owner">Owner: {freshness.owner_name}</span><span class="identity-verified">{freshnessPresentation.phrase}</span></span>{/if}
		</button>
		{#if freshnessPresentation?.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		{#if presence}{@render presence()}{/if}
	</div>
{#if freshness && freshnessPresentation}
	<div class="document-metadata" class:expanded={detailsOpen}>
		<div class="metadata-owner">Owner: {freshness.owner_name}</div>
		{#if freshnessPresentation.datetime}
			<time datetime={freshnessPresentation.datetime}>
				{freshnessPresentation.phrase}
				{#if freshnessPresentation.dateText}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}
			</time>
		{:else}
			<span class="metadata-secondary">{freshnessPresentation.phrase}</span>
		{/if}
		{#if freshnessPresentation.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		{#if shouldOfferVerification(freshness)}
			<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying}>Mark verified</Button></span>
		{/if}
		<!-- Verification recovery is rendered outside the static metadata disclosure. -->
	</div>
{/if}

<DocLiveStatus session={liveSession} {openRefusal} {liveUnavailable} {tabText} {onReopen} {onRead}
	connectionStatusId={liveConnectionDescriptionId} />
<span class:sr-only={!verificationError && !placementAnnouncement} class:metadata-error={!!verificationError} role="status">{verificationError || verificationNotice || placementAnnouncement || staleAnnouncement}</span>
{#if readonly && ready}
	<div class="readonly-note">Read-only — this file is owned by the system bootloader.</div>
{/if}
{#if commentCount || orphanedCount || artifactId || (stale && ready)}
	<div class="document-status-row">
		<span class="feedback-controls"><DocFeedbackStatus
			bind:this={feedbackStatus}
			{artifactId}
			{targetInstanceId}
			{onFeedbackChanged}
		/></span>
		{#if stale && ready}
			<button type="button" class="doc-tool warn stale-control" onclick={() => onReload?.()}>File changed · Reload</button>
		{/if}
		<button type="button" class="doc-tool comment-status" bind:this={commentStatus} aria-expanded={commentsOpen} disabled={!ready || commentsLoading}
			aria-label={commentsLoading ? 'Loading comments' : `${commentCount} comment${commentCount === 1 ? '' : 's'}`}
			aria-describedby={orphanedCount ? unplacedId : undefined}
			title={commentsLoading ? 'Comments are loading' : orphanedCount ? unplacedPhrase : undefined} onclick={(event) => onOpenComments(event.currentTarget)}>
			<span class="icon-anchor"><svg class="tool-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z" /></svg>{#if orphanedCount}<span class="unplaced-dot" aria-hidden="true"></span>{/if}</span>
			<span class="count">{commentsLoading ? '…' : commentCount}</span>
			{#if orphanedCount}<span id={unplacedId} class="sr-only">{unplacedPhrase}</span>{/if}
		</button>
		{#if orphanedCount && placementReloadVisible}
			<button type="button" class="doc-tool orphan-reload" aria-label="Reload comment placements"
				aria-disabled={placementReloadUnavailable || placementReloading ? 'true' : undefined}
				aria-describedby={placementReloadUnavailable ? liveConnectionDescriptionId : undefined}
				onclick={reloadPlacements}>Place again</button>
		{/if}
		{#if artifactId}<DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} />{/if}
	</div>
{/if}
</header>

<style>
	.document-header { grid-area: header; min-width: 0; flex: none; display: flex; flex-direction: column; gap: 6px; padding-left: var(--doc-phone-inset, var(--doc-rail, 44px)); background: var(--bg-primary); }
	.document-identity { display: flex; align-items: center; gap: 6px; min-width: 0; }
	.identity-details { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; border: 0; padding: 2px 0; background: none; color: var(--text-primary); cursor: pointer; text-align: left; }
	.document-title { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--text-subhead); font-weight: var(--fw-semibold); }
	.identity-age { display: none; }
	.feedback-controls { display: contents; }
	.identity-details:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--radius-sm); }
	.document-metadata, .document-status-row { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	/* One toolbar: every control is the same quiet ghost button in the app's UI type. */
	.document-status-row { gap: 4px; }
	.document-status-row :global(.doc-tool) { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 28px; padding: 0 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap; cursor: pointer; }
	.document-status-row :global(.doc-tool:hover:not(:disabled)) { background: var(--bg-tertiary); color: var(--text-primary); }
	.document-status-row :global(.doc-tool:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
	.document-status-row :global(.doc-tool:disabled) { opacity: 0.5; cursor: not-allowed; }
	.document-status-row :global(.doc-tool.warn) { background: var(--amber-bg); color: var(--amber-text); }
	.document-status-row :global(.doc-tool.warn:hover:not(:disabled)) { background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber-text); }
	.document-status-row :global(.doc-tool[aria-disabled='true']) { opacity: 0.65; cursor: not-allowed; }
	.tool-icon { width: 14px; height: 14px; flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; }
	.icon-anchor { position: relative; display: inline-flex; }
	.unplaced-dot { position: absolute; top: -3px; right: -4px; width: 7px; height: 7px; border-radius: var(--radius-circle); background: var(--amber); box-shadow: 0 0 0 2px var(--bg-primary); }
	.document-metadata { color: var(--text-secondary); font-size: var(--text-footnote); }
	.metadata-owner { flex: 1 1 10rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.verification-action { margin-left: -10px; }
	.metadata-secondary, .document-metadata time { color: var(--text-secondary); }
	.metadata-error { color: var(--red); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	/* After the base rules above: equal specificity, so the later rule wins at phone width. */
	@container doc-canvas (max-width: 600px) {
		.identity-details { min-height: 44px; justify-content: center; }
		.identity-age { display: flex; width: 100%; gap: 6px; color: var(--text-secondary); font-size: var(--text-caption); }
		.identity-owner { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		.identity-verified { flex: none; white-space: nowrap; }
		.document-metadata:not(.expanded) { display: none; }
		.document-status-row { flex-wrap: wrap; }
		.document-status-row :global(.doc-tool) { min-height: 44px; padding: 0 12px; }
		.feedback-controls :global(> .btn) { display: block; flex: 1; min-width: 0; min-height: 44px; overflow: hidden; text-overflow: ellipsis; }
		.feedback-controls :global(.undelivered:not(.idle)) { flex: 1 1 100%; }
	}
</style>
