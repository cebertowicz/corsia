<script lang="ts">
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
	import DocReviewControls from './DocReviewControls.svelte';
	import CommentsPill from './comments/CommentsPill.svelte';
	import { Button, StatePill } from '$lib/components/ui';
	import { formatFreshness, type DocumentFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';

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
	// The unsent comments, for the pill's dot and accessible summary; the batch itself is the one source.
	const unsent = $derived(feedbackStatus?.controller().pendingCount ?? 0);
	// A live region only announces a change, so the stale state needs text that appears when it starts.
	const staleAnnouncement = $derived(stale && ready ? 'File changed since you opened it. Reload to see the latest.' : '');
	const freshnessPresentation = $derived(freshness ? formatFreshness(freshness) : null);
	// The live-status row renders the stable connection description for unavailable actions.
	const connectionDescriptionId = $derived(liveConnectionDescriptionId(liveSession?.artifactId));
	$effect(() => {
		artifactId;
		detailsOpen = false;
		verificationError = '';
		verificationNotice = '';
	});

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
	export function feedback(): DocFeedback | undefined { return feedbackStatus?.controller(); }
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
		<span class="document-tools">
			{#if stale && ready}<button type="button" class="doc-tool warn stale-control" onclick={() => onReload?.()}>File changed · Reload</button>{/if}
			<CommentsPill class="doc-tool comment-status" count={commentCount} {unsent} unplaced={orphanedCount}
				loading={commentsLoading} expanded={commentsOpen} disabled={!ready || commentsLoading} onOpen={onOpenComments} />
			{#if artifactId}<DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} />{/if}
		</span>
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
	connectionStatusId={connectionDescriptionId} />
<span class:sr-only={!verificationError} class:metadata-error={!!verificationError} role="status">{verificationError || verificationNotice || staleAnnouncement}</span>
{#if readonly && ready}
	<div class="readonly-note">Read-only — this file is owned by the system bootloader.</div>
{/if}
{#if commentCount || orphanedCount || artifactId || (stale && ready)}
	<div class="document-status-row document-notices">
		<span class="feedback-controls"><DocFeedbackStatus bind:this={feedbackStatus} {artifactId} {targetInstanceId} {onFeedbackChanged} withSend={false} /></span>
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
		.feedback-controls :global(.undelivered:not(.idle)) { flex: 1 1 100%; }
	}

	/* Slice-one review header: the title is already named by the tab or app bar, so only document tools remain in the slim row. */
	/* Where the plan pane overlays the canvas (PaneShell), the header's content ends before it instead of under it: the tools, both ask lines and the notices.
	   The header is a container, so the ask's line 1 can rearrange when that leaves it too narrow (ReviewBar). */
	.document-header { gap: var(--space-1); border-bottom: 1px solid var(--border-subtle); padding-bottom: var(--space-1); padding-inline-end: var(--pane-overlay-inset, 0px); container: doc-header / inline-size; }
	.document-identity { justify-content: flex-end; }
	.identity-details { display: none; }
	.document-tools { flex: none; display: inline-flex; align-items: center; gap: var(--space-1); }
	.document-notices:not(:has(.undelivered:not(.idle))) { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); }
	.document-header :global(.doc-tool) { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 28px; padding: 0 8px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap; cursor: pointer; }
	.document-header :global(.doc-tool:hover:not(:disabled)) { background: var(--bg-tertiary); color: var(--text-primary); }
	.document-header :global(.doc-tool:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
	.document-header :global(.doc-tool:disabled) { opacity: 0.5; cursor: not-allowed; }
	.document-header :global(.doc-tool.warn) { background: var(--amber-bg); color: var(--amber-text); }
	.document-header :global(.doc-tool.warn:hover:not(:disabled)) { background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber-text); }
	.document-header :global(.doc-tool[aria-disabled='true']) { opacity: 0.65; cursor: not-allowed; }
	@container doc-canvas (max-width: 600px) {
		.identity-details:has(.identity-age) { display: flex; }
		.document-title { display: none; }
		.document-header :global(.doc-tool) { min-height: 44px; padding: 0 12px; }
	}
	/* When an ask exists, its first line and the tool cluster share one row; the second ask line spans the canvas. */
	.document-header:has(:global(.rb-slot)) { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: start; column-gap: var(--space-1); }
	.document-header:has(:global(.rb-slot)) > :global(*) { grid-column: 1 / -1; }
	.document-header:has(:global(.rb-slot)) > :global(.rb-slot) { grid-column: 1 / -1; grid-row: 1 / 3; }
	.document-header:has(:global(.rb-slot)) > .document-identity { display: contents; }
	.document-header:has(:global(.rb-slot)) .identity-details { grid-column: 1 / -1; grid-row: 3; }
	.document-header:has(:global(.rb-slot)) :global(.presence-row) { grid-column: 2; grid-row: 1; align-self: start; min-height: var(--space-7); position: relative; z-index: 1; }
	.document-header:has(:global(.rb-slot)) .document-tools { grid-column: 3; grid-row: 1; align-self: start; min-height: var(--space-7); position: relative; z-index: 1; }
	.document-header:has(:global(.rb-slot)) :global(.doc-tool) { min-height: var(--space-7); }
	@media (any-pointer: coarse) {
		@container doc-canvas (max-width: 600px) {
			.document-header:has(:global(.review-bar.ask:not(.expanded))) .identity-details:has(.identity-age) { display: none; }
			.document-header:has(:global(.rb-slot)) { align-items: center; }
			.document-header:has(:global(.rb-slot)) :global(.doc-tool) { min-height: 44px; }
		}
	}
</style>
