<script lang="ts">
	import type { Snippet } from 'svelte';
	import DocFeedbackControls from './DocFeedbackControls.svelte';
	import CommentsPill from './comments/CommentsPill.svelte';
	import DocOwnerControl from './DocOwnerControl.svelte';
	import DocReviewControls from './DocReviewControls.svelte';
	import { Button, StatePill, Toggle } from '$lib/components/ui';
	import { shouldOfferVerification, type DocumentFreshness, type FreshnessPresentation } from '$lib/docFreshness';
	import type { DocOwnership } from '$lib/api/docOwnership';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { deliverCommentsImmediately, changeImmediateDelivery } from '$lib/commentDeliveryPreference';

	/** The document column's row above the phone width: the count, delivery and review tools, then who owns the file and how fresh it is. DocViewerStatus holds the state; this only draws it. */
	let {
		presence, heldFeedback, artifactId, urgent, onReload, saveStatus = 'idle', offScreen = false, commentCount, unreadCount = 0, unsent,
		commentsLoading = false, ready, commentsOpen = false, onOpenComments, pillEl = $bindable(), viewingThreadId = null, reviewKey, restoreTo,
		historyAvailable = false, historyOpen = false, onOpenHistory, freshness, freshnessPresentation, ownership = null, headerStandIn = false,
		onOwnershipChanged, markVerified, verifying, readonly, resolvedCount = 0, resolvedOpen = false, onOpenResolved,
	}: {
		presence?: Snippet; heldFeedback: DocFeedback | undefined; artifactId: string | null; urgent: boolean; onReload?: () => void;
		saveStatus?: 'idle' | 'saving' | 'saved'; offScreen?: boolean; commentCount: number; unreadCount?: number; unsent: number;
		commentsLoading?: boolean; ready: boolean; commentsOpen?: boolean; onOpenComments: (opener: HTMLElement) => void; pillEl?: HTMLButtonElement;
		viewingThreadId?: string | null; reviewKey: string | null; restoreTo: () => HTMLElement | null;
		historyAvailable?: boolean; historyOpen?: boolean; onOpenHistory?: () => void; freshness: DocumentFreshness | null;
		freshnessPresentation: FreshnessPresentation | null; ownership?: DocOwnership | null; headerStandIn?: boolean;
		onOwnershipChanged?: (value: DocOwnership) => void; markVerified: () => Promise<void>; verifying: boolean; readonly: boolean;
		resolvedCount?: number; resolvedOpen?: boolean; onOpenResolved?: (opener: HTMLElement) => void;
	} = $props();

	// At a narrow canvas the owner and freshness wait behind the identity toggle, as they did before the one-row header.
	let detailsOpen = $state(false);
	let detailsEl = $state<HTMLButtonElement | null>(null);
	const showOwnership = $derived(!!(ownership && artifactId));
	$effect(() => { artifactId; detailsOpen = false; });
	function closeOnEscape(event: KeyboardEvent) {
		if (event.key !== 'Escape' || !detailsOpen) return;
		event.preventDefault();
		event.stopPropagation();
		detailsOpen = false;
		detailsEl?.focus();
	}
</script>

<!-- The document column's row above the phone width: two groups, the only places the cluster may break: what is being sent and counted, then how it is delivered and reviewed. -->
<div class="document-identity">
	<button type="button" class="identity-details" bind:this={detailsEl} aria-label="Document details" aria-expanded={detailsOpen} onclick={() => detailsOpen = !detailsOpen} onkeydown={closeOnEscape}>
		{#if freshness && freshnessPresentation}<span class="identity-age" title={showOwnership ? freshnessPresentation.phrase : `Owner: ${freshness.owner_name} · ${freshnessPresentation.phrase}`}>{#if !showOwnership}<span class="identity-owner">Owner: {freshness.owner_name}</span>{/if}<span class="identity-verified">{freshnessPresentation.phrase}</span></span>{/if}
	</button>
	{#if freshnessPresentation?.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
	{#if presence}{@render presence()}{/if}
	<span class="document-tools document-status-row">
		<span class="tool-group tool-send">
		{#if artifactId && heldFeedback}<span class="feedback-controls"><DocFeedbackControls feedback={heldFeedback} lane="document-held" announce={false} /></span>{/if}
		{#if urgent}<button type="button" class="doc-tool warn stale-control" data-file-changed onclick={() => onReload?.()}>File changed · Reload</button>{/if}
		{#if saveStatus !== 'idle'}<span class="save-ghost" class:saving={saveStatus === 'saving'}>{saveStatus === 'saving' ? 'Saving…' : '✓ Saved'}</span>{/if}
		<!-- Off screen the document offers no Comments, as the bar holds none then: nothing is opened or read from a hidden document. -->
		{#if !offScreen}<CommentsPill class="doc-tool comment-status" data-comments-entry bind:element={pillEl} count={commentCount} unread={unreadCount} {unsent}
			loading={commentsLoading} unknown={!ready} expanded={commentsOpen} disabled={!ready || commentsLoading} onOpen={onOpenComments} />{/if}
		{#if !offScreen && resolvedCount && onOpenResolved}<button type="button" class="doc-tool resolved-tool" data-resolved-tool aria-expanded={commentsOpen && resolvedOpen} onclick={(event) => onOpenResolved(event.currentTarget)}>Resolved · {resolvedCount}</button>{/if}
		</span>
		<span class="tool-group tool-more">
		{#if artifactId}
			<span class="tool-group tool-deliver">
				<span class="delivery-toggle" title="When off, comments send when you close the document or pause for two minutes.">
					<span>Deliver immediately</span>
					<Toggle size="sm" touch checked={$deliverCommentsImmediately} onchange={(value) => changeImmediateDelivery(value, heldFeedback)} ariaLabel="Deliver comments immediately" />
				</span>
			</span>
		{/if}
		<span class="tool-group tool-review">
		{#if artifactId}
			<DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewKey} {restoreTo} />
		{/if}
		{#if historyAvailable && onOpenHistory}<button type="button" class="doc-tool history-tool" aria-label="History" title="History" aria-expanded={historyOpen} onclick={onOpenHistory}>
			<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.35-5.65L4 8.5M4 4.5v4h4M12 7.5V12l3 2" /></svg>
			<span class="label">History</span>
		</button>{/if}
		</span>
		</span>
	</span>
</div>
{#if (freshness && freshnessPresentation) || (ownership && artifactId)}
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="document-metadata" class:expanded={detailsOpen} onkeydown={closeOnEscape} class:has-ownership={!!(ownership && artifactId)} class:held={headerStandIn} data-ownership-row={(ownership && artifactId) || undefined}>
	{#if ownership && artifactId}<div class="metadata-owner">{#key artifactId}<DocOwnerControl {artifactId} {ownership} inert={headerStandIn} onChange={onOwnershipChanged ?? (() => {})} />{/key}</div>
	{:else if freshness}<div class="metadata-owner">Owner: {freshness.owner_name}</div>{/if}
	{#if freshnessPresentation?.datetime}
		<time datetime={freshnessPresentation.datetime}>
			{freshnessPresentation.phrase}
			{#if freshnessPresentation.dateText}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}
		</time>
	{:else if freshnessPresentation}
		<span class="metadata-secondary">{freshnessPresentation.phrase}</span>
	{/if}
	{#if freshnessPresentation?.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
	{#if freshness && shouldOfferVerification(freshness)}
		<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying} disabled={headerStandIn}>Mark verified</Button></span>
	{/if}
</div>
{/if}
{#if readonly && ready}<div class="readonly-note">Read-only.</div>{/if}

<style>
	.document-identity { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: var(--chip-inline-padding); min-width: 0; }
	.feedback-controls { display: contents; }
	.document-metadata, .document-status-row { min-width: 0; display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); }
	/* One toolbar: every control is the same quiet ghost button in the app's UI type. */
	.document-status-row { gap: var(--space-1); }
	.document-status-row :global(.doc-tool) { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: var(--chip-inline-padding); height: calc(var(--space-7) - var(--space-1)); padding: 0 var(--space-2); border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font-family: var(--font-sans); font-size: var(--text-footnote); font-weight: var(--fw-medium); white-space: nowrap; cursor: pointer; }
	.document-status-row :global(.doc-tool:hover:not(:disabled)) { background: var(--bg-tertiary); color: var(--text-primary); }
	.document-status-row :global(.doc-tool:focus-visible) { outline: 2px solid var(--accent); outline-offset: -2px; }
	.document-status-row :global(.doc-tool:disabled) { opacity: 0.5; cursor: not-allowed; }
	.document-status-row :global(.doc-tool.warn) { background: var(--amber-bg); color: var(--amber-text); }
	.document-status-row :global(.doc-tool.warn:hover:not(:disabled)) { background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber-text); }
	.document-status-row :global(.doc-tool[aria-disabled='true']) { opacity: 0.65; cursor: not-allowed; }
	.document-metadata { color: var(--text-secondary); font-size: var(--text-footnote); }
	.metadata-owner { flex: 1 1 10rem; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	/* The owner and supervisor items draw their own hover fill and focus ring, which a clipped cell would cut off. */
	.has-ownership .metadata-owner { overflow: visible; }
	.verification-action { flex: none; display: inline-flex; min-height: inherit; }
	.document-metadata .verification-action { margin-left: calc(-1 * var(--button-pad-x-sm)); }
	/* A remembered row waiting for its load is drawn exactly as the loaded one, so its controls are neither dimmed nor filled on hover; they only don't act yet. */
	.document-metadata.held :global(button:disabled) { opacity: 1; cursor: default; }
	.document-metadata.held :global(button.item:disabled:hover) { background: transparent; color: var(--text-secondary); }
	.metadata-secondary, .document-metadata time { color: var(--text-secondary); }
	/* The cluster keeps one row while it fits and wraps inside the header when it cannot, whatever it holds: a held sender, a reason, Reload, a long count. */
	.document-tools { position: relative; z-index: 1; flex: none; display: inline-flex; flex-wrap: wrap; max-inline-size: 100%; justify-content: flex-end; align-items: center; gap: var(--space-1); }
	/* The cluster breaks only where content does not fit, in this order: between [Send · count] and the rest; inside the rest, between [Deliver immediately · switch] and [reviewers · Request review · History]; and, only when [Send · count] alone is wider than the header, between Send and the count. The label never leaves its switch, and the reviewers, Request review and History stay on one row. */
	.tool-group { display: inline-flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: var(--space-1); max-inline-size: 100%; }
	.tool-deliver, .tool-review { flex-wrap: nowrap; }
	.tool-review:not(:has(:global(*))) { display: none; }
	/* The label sits the chip padding from the switch at every width: the switch's touch size is a hit area inside the switch, so it never moves the label. */
	.delivery-toggle { display: inline-flex; align-items: center; gap: var(--chip-inline-padding); color: var(--text-secondary); font-size: var(--text-footnote); white-space: nowrap; }
	/* History is a text button on a wide canvas and an icon-only button once the canvas is 600px or narrower, like Request review beside it. */
	.history-tool svg { display: none; width: var(--text-callout); height: var(--text-callout); fill: none; stroke: currentColor; stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; flex: none; }
	@container doc-header (max-width: 32.5rem) {
		.document-tools, .tool-group { gap: 0; }
	}
	/* The identity toggle shows only at a narrow canvas, and only when there is an age line to hold. */
	.identity-details { flex: 1; min-width: 0; display: none; flex-direction: column; align-items: flex-start; border: 0; padding: var(--space-px-2) 0; background: none; color: var(--text-primary); cursor: pointer; text-align: left; }
	.identity-details:focus-visible { outline: 2px solid var(--accent); outline-offset: var(--space-px-2); border-radius: var(--radius-sm); }
	.identity-age { display: none; }
	@container doc-canvas (max-width: 37.5rem) {
		.identity-details:has(.identity-age) { display: flex; min-height: var(--touch-target); justify-content: center; }
		.identity-age { display: flex; width: 100%; gap: var(--chip-inline-padding); color: var(--text-secondary); font-size: var(--text-caption); container: identity-age / inline-size; }
		.identity-owner { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		/* The line gets what the tools leave it: the verified phrase truncates inside it, and the owner, which would show as a clipped fragment, steps aside first. */
		.identity-verified { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		@container identity-age (width < 7.5rem) { .identity-owner { display: none; } }
		/* Owner and supervisor stay in view at every width; the rest of the row waits behind Document details. */
		.document-metadata:not(.expanded):not(.has-ownership), .document-metadata.has-ownership:not(.expanded) > :not(.metadata-owner) { display: none; }
		.has-ownership .metadata-owner { flex-basis: 100%; }
		.history-tool svg { display: block; }
		/* The count and Resolved sit closer, so "N comments · N unread" and "Resolved · N" keep one row in a narrow header. */
		.tool-send :global(.comment-status:has(+ .resolved-tool)) { padding-inline-end: var(--space-1); }
		.tool-send .resolved-tool { padding-inline-start: var(--space-1); }
		.history-tool .label { display: none; }
		.document-status-row { flex-wrap: wrap; }
		.document-status-row :global(.doc-tool) { min-height: var(--touch-target); padding: 0 var(--space-3); }
		.delivery-toggle { min-height: var(--touch-target); --toggle-hit: var(--touch-target); }
		/* The tool row cannot shrink, and with Reload, the count and the review controls the save pill pushes it past the left edge: the save state is transient and Reload is not, so the pill yields. */
		.stale-control ~ .save-ghost { display: none; }
		.feedback-controls :global(.undelivered:not(.idle)) { flex: 1 1 100%; }
	}
</style>
