<script lang="ts">
	import DocFeedbackStatus from './DocFeedbackStatus.svelte';
	import DocOwnerControl from './DocOwnerControl.svelte';
	import type { DocOwnership } from '$lib/api/docOwnership';
	import DocLiveStatus from './DocLiveStatus.svelte';
	import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
	import type { DocOpenRefusal, LiveUnavailable } from '$lib/docOpenRefusal';
	import DocReviewControls from './DocReviewControls.svelte';
	import CommentsPill from './comments/CommentsPill.svelte';
	import { Button, StatePill, Toggle } from '$lib/components/ui';
	import { formatFreshness, type DocumentFreshness, shouldOfferVerification } from '$lib/docFreshness';
	import type { CommentFeedback } from '$lib/api/docs';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	import { liveConnectionDescriptionId } from '$lib/liveConnectionDescription';
	import { deliverCommentsImmediately, setDeliverCommentsImmediately } from '$lib/commentDeliveryPreference';

	let {
		title = 'Document',
		presence,
		reviewBar,
		commentsOpen = false,
		commentsLoading = false,
		saveStatus = 'idle',
		ready,
		stale,
		readonly,
		commentCount,
		unreadCount = 0,
		orphanedCount,
		artifactId,
		targetInstanceId,
		viewingThreadId = null,
		reviewTargetKey = null,
		onReload,
		onOpenComments,
		freshness,
		ownership = null,
		onOwnershipChanged,
		onFeedbackChanged,
		onMarkVerified,
		liveSession = null,
		openRefusal = null,
		liveUnavailable = null,
		tabText = '',
		onReopen,
		onRead,
		imageOpen = false,
		activityRoot = null,
		historyAvailable = false,
		historyOpen = false,
		onOpenHistory,
	}: {
		title?: string;
		presence?: import('svelte').Snippet;
		/** The review bar sits first in the header, so its buttons come before the document in tab order. */
		reviewBar?: import('svelte').Snippet;
		commentsOpen?: boolean;
		commentsLoading?: boolean;
		saveStatus?: 'idle' | 'saving' | 'saved';
		ready: boolean;
		stale: boolean;
		readonly: boolean;
		commentCount: number;
		unreadCount?: number;
		orphanedCount: number;
		artifactId: string | null;
		targetInstanceId: string | null;
		viewingThreadId?: string | null;
		reviewTargetKey?: string | null;
		onReload?: () => void;
		onOpenComments: (opener: HTMLElement) => void;
		freshness: DocumentFreshness | null;
		ownership?: DocOwnership | null;
		onOwnershipChanged?: (value: DocOwnership) => void;
		onFeedbackChanged?: () => void | Promise<void>;
		onMarkVerified?: () => Promise<void>;
		liveSession?: LiveDocumentSession | null;
		openRefusal?: DocOpenRefusal | null;
		liveUnavailable?: LiveUnavailable | null;
		tabText?: string;
		onReopen?: () => void;
		onRead?: () => void;
		/** An image of this document covers it; its viewer owns the image lane's Send until it is left. */
		imageOpen?: boolean;
		activityRoot?: HTMLElement | null;
		/** The file has recorded versions (slice 1: instruction files). Without them there is no History, and no count. */
		historyAvailable?: boolean;
		historyOpen?: boolean;
		onOpenHistory?: () => void;
	} = $props();

	let feedbackStatus = $state<DocFeedbackStatus>();
	let detailsOpen = $state(false);
	let ownershipInDetails = $state(true);
	let compactOwnerOnly = $state(true);
	let compactOwnerOpen = $state(false);
	let ownerAnchor = $state<HTMLButtonElement | null>(null);
	$effect(() => { if (!compactOwnerOpen) ownerAnchor = null; });

	function openDetails(button: HTMLButtonElement) {
		const owner = button.parentElement?.querySelector<HTMLElement>('.identity-owner');
		if (ownership && artifactId && owner && getComputedStyle(owner).display === 'none') {
			ownerAnchor = button;
			compactOwnerOpen = true;
		} else detailsOpen = !detailsOpen;
	}
	let ownershipSaving = $state(false);
	let ownershipError = $state('');
	let ownershipErrorFor = $state<'owner' | 'supervisor' | null>(null);
	let ownershipPending = $state<{ kind: 'owner' | 'supervisor'; id: string; name: string } | null>(null);

	// Measure the complete status content, including the full ownership label.
	// The detached visual probe is independent of the currently selected layout,
	// so changing to Details does not change the threshold used to return.
	function ownershipFit(header: HTMLElement) {
		let frame = 0;
		const measure = () => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(() => {
				const metadata = header.querySelector<HTMLElement>('.document-metadata.has-ownership');
				if (!metadata) return;
				const probe = metadata.cloneNode(true) as HTMLElement;
				probe.classList.remove('expanded');
				Object.assign(probe.style, { position: 'fixed', left: '-10000px', top: '0', display: 'flex', width: 'max-content', maxWidth: 'none', overflow: 'visible', flexWrap: 'nowrap', visibility: 'hidden', font: getComputedStyle(metadata).font });
				const owner = probe.querySelector<HTMLElement>('.metadata-owner');
				const button = probe.querySelector<HTMLElement>('button.identity');
				if (owner) Object.assign(owner.style, { flex: 'none', overflow: 'visible' });
				if (button) { button.classList.remove('compact'); Object.assign(button.style, { width: 'max-content', maxWidth: 'none' }); }
				probe.querySelectorAll<HTMLElement>('.identity-name').forEach(name => { name.style.overflow = 'visible'; name.style.minWidth = 'max-content'; });
				document.body.appendChild(probe);
				const needed = probe.getBoundingClientRect().width;
				probe.remove();
				const style = getComputedStyle(header);
				const available = header.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
				ownershipInDetails = available < needed;
				const slot = header.querySelector<HTMLElement>('.identity-owner');
				const compactButton = slot?.querySelector<HTMLElement>('button.identity');
				if (slot && compactButton && slot.getBoundingClientRect().width > 0) {
					const compactProbe = compactButton.cloneNode(true) as HTMLElement;
					compactProbe.classList.remove('compact');
					Object.assign(compactProbe.style, { position: 'fixed', left: '-10000px', top: '0', width: 'max-content', maxWidth: 'none', overflow: 'visible', visibility: 'hidden', font: getComputedStyle(compactButton).font });
					compactProbe.querySelectorAll<HTMLElement>('.identity-name').forEach(name => { name.style.overflow = 'visible'; name.style.minWidth = 'max-content'; });
					document.body.appendChild(compactProbe);
					compactOwnerOnly = slot.getBoundingClientRect().width < compactProbe.getBoundingClientRect().width;
					compactProbe.remove();
				}
			});
		};
		const resize = new ResizeObserver(measure);
		resize.observe(header);
		const mutations = new MutationObserver(measure);
		mutations.observe(header, { childList: true, characterData: true, subtree: true });
		measure();
		return { destroy() { cancelAnimationFrame(frame); resize.disconnect(); mutations.disconnect(); } };
	}

	let verifying = $state(false);
	let verificationError = $state('');
	let verificationNotice = $state('');
	// The unsent comments, for the pill's dot and accessible summary; the batch itself is the one source.
	const unsent = $derived(feedbackStatus?.controller().pendingCount ?? 0);
	// Delivery controls mount in the identity row so Send holds the compact header line.
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

	async function changeImmediateDelivery(value: boolean): Promise<void> {
		if (!(await setDeliverCommentsImmediately(value))) return;
		const held = feedbackStatus?.controller();
		if (value && held && held.documentHeld > 0) void held.sendDocument();
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
			const toggle = element.querySelector<HTMLButtonElement>('.details-toggle');
			if (event.target !== toggle && !element.querySelector('.document-metadata')?.contains(event.target)) return;
			event.preventDefault();
			event.stopPropagation();
			detailsOpen = false;
			toggle?.focus();
		};
		element.addEventListener('keydown', onKey);
		return { destroy() { element.removeEventListener('keydown', onKey); } };
	}

	// The viewer reads the document's feedback once, often before this row's status child exists (it mounts only once
	// there is something to show). The latest read waits here and goes to the matching child when it mounts, so the
	// initial state is never dropped and no second read is needed.
	let buffered: { id: string; feedback: CommentFeedback } | null = null;
	$effect(() => {
		const child = feedbackStatus;
		const current = artifactId;
		if (!buffered) return;
		if (buffered.id !== current) buffered = null;
		else if (child) child.sync(buffered.id, buffered.feedback);
	});

	// An image exit that never completed (a reload or teardown) leaves its lane held with no viewer to send it. The
	// next time this document mounts uncovered, it sends that remainder once, before the lane can stay hidden.
	let recoveredFor: string | null = null;
	$effect(() => {
		const held = feedbackStatus?.controller();
		if (!held || imageOpen || !held.artifactId || held.imageHeld === 0) return;
		if (recoveredFor === held.artifactId) return;
		recoveredFor = held.artifactId;
		void held.sendImage();
	});

	export function commentCreated(): void { feedbackStatus?.commentCreated(); }
	export function sync(id: string, feedback: CommentFeedback): void {
		buffered = { id, feedback };
		feedbackStatus?.sync(id, feedback);
	}
	export function feedback(): DocFeedback | undefined { return feedbackStatus?.controller(); }
</script>

<header class="document-header" use:detailsKeys use:ownershipFit>
	{#if reviewBar}{@render reviewBar()}{/if}
	<div class="document-identity">
		<div class="identity-details">
			<span class="document-title" title={title}>{title}</span>
			{#if (ownership && artifactId) || (freshness && freshnessPresentation)}<span class="identity-age"><span class="identity-owner" class:has-freshness={!!freshnessPresentation}>{#if ownership && artifactId}<DocOwnerControl {artifactId} {ownership} compact={ownershipInDetails || compactOwnerOnly} bind:open={compactOwnerOpen} externalAnchor={ownerAnchor} bind:saving={ownershipSaving} bind:error={ownershipError} bind:errorFor={ownershipErrorFor} bind:pending={ownershipPending} onChange={(value) => onOwnershipChanged?.(value)} />{:else if freshness}Owner: {freshness.owner_name}{/if}</span>{#if freshnessPresentation}<button type="button" class="identity-verified details-toggle" aria-label="Document details" aria-expanded={detailsOpen || compactOwnerOpen} onclick={(event) => openDetails(event.currentTarget)}>{freshnessPresentation.phrase}</button>{/if}</span>{/if}
		</div>
		{#if freshnessPresentation?.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
		{#if presence}{@render presence()}{/if}
		<span class="document-tools document-status-row">
			{#if artifactId}
				<!-- Send belongs to this compact tool row with the comment count, delivery preference, and review action. -->
				<span class="feedback-controls"><DocFeedbackStatus bind:this={feedbackStatus} {artifactId} {targetInstanceId} {onFeedbackChanged} {activityRoot} lane="document-held" /></span>
			{/if}
			{#if stale && ready}<button type="button" class="doc-tool warn stale-control" onclick={() => onReload?.()}>File changed · Reload</button>{/if}
			{#if saveStatus !== 'idle'}<span class="save-ghost" class:saving={saveStatus === 'saving'}>{saveStatus === 'saving' ? 'Saving…' : '✓ Saved'}</span>{/if}
			<CommentsPill class="doc-tool comment-status" count={commentCount} unread={unreadCount} {unsent} unplaced={orphanedCount}
				loading={commentsLoading} unknown={!ready} expanded={commentsOpen} disabled={!ready || commentsLoading} onOpen={onOpenComments} />
			{#if artifactId}
				<span class="delivery-toggle" title="Deliver immediately. When off, comments send when you close the document or pause for two minutes.">
					<span>Deliver immediately</span>
					<Toggle size="sm" checked={$deliverCommentsImmediately} onchange={changeImmediateDelivery} ariaLabel="Deliver comments immediately" />
				</span>
				<DocReviewControls {artifactId} {viewingThreadId} targetKey={reviewTargetKey ?? targetInstanceId} />
			{/if}
			{#if historyAvailable && onOpenHistory}<button type="button" class="doc-tool history-tool" aria-label="History" title="History" aria-expanded={historyOpen} onclick={onOpenHistory}>
				<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.35-5.65L4 8.5M4 4.5v4h4M12 7.5V12l3 2" /></svg>
				<span class="label">History</span>
			</button>{/if}
		</span>
		<!-- Document-held sender is mounted above, in the identity row. -->
	</div>
	{#if (freshness && freshnessPresentation) || (ownership && artifactId)}
		<div class="document-metadata" class:expanded={detailsOpen} class:has-ownership={!!ownership}>
			{#if ownership && artifactId}<div class="metadata-owner"><DocOwnerControl {artifactId} {ownership} compact={ownershipInDetails} bind:saving={ownershipSaving} bind:error={ownershipError} bind:errorFor={ownershipErrorFor} bind:pending={ownershipPending} onChange={(value) => onOwnershipChanged?.(value)} /></div>
			{:else if freshness}<div class="metadata-owner">Owner: {freshness.owner_name}</div>{/if}
			{#if freshness && freshnessPresentation}
				{#if freshnessPresentation.datetime}<time datetime={freshnessPresentation.datetime}>{freshnessPresentation.phrase}{#if freshnessPresentation.dateText}<span class="sr-only"> ({freshnessPresentation.dateText})</span>{/if}</time>
				{:else}<span class="metadata-secondary">{freshnessPresentation.phrase}</span>{/if}
				{#if freshnessPresentation.overdue}<StatePill state="warning" size="sm" label="Overdue" />{/if}
				{#if shouldOfferVerification(freshness)}<span class="verification-action"><Button size="sm" variant="ghost" onclick={markVerified} loading={verifying}>Mark verified</Button></span>{/if}
			{/if}
		</div>
	{/if}
<DocLiveStatus session={liveSession} {openRefusal} {liveUnavailable} {tabText} {onReopen} {onRead}
	connectionStatusId={connectionDescriptionId} />
<span class="metadata-status" class:sr-only={!verificationError} class:metadata-error={!!verificationError} role="status">{verificationError || verificationNotice || staleAnnouncement}</span>
{#if readonly && ready}
	<div class="readonly-note">Read-only.</div>
{/if}
<!-- Delivery controls above remain mounted with the document identity so they never force a second header row. -->
</header>

<style>
	.document-header { grid-area: header; min-width: 0; flex: none; display: flex; flex-direction: column; gap: 6px; padding-left: var(--doc-inset, 24px); background: var(--bg-primary); }
	.document-identity { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-width: 0; }
	/* Match the original button's font after splitting its two actions. */
	.identity-details { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; border: 0; padding: 2px 0; background: none; color: var(--text-primary); font: 13.3333px Arial; cursor: pointer; text-align: left; }
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
	.document-tools { position: relative; z-index: 1; flex: none; display: inline-flex; align-items: center; gap: var(--space-1); }
	/* Send holds the identity line at compact widths; the existing tool cluster is the element that yields and wraps. */
	.feedback-controls { flex: none; min-width: max-content; }
	.delivery-toggle { display: inline-flex; align-items: center; gap: var(--chip-inline-padding); color: var(--text-secondary); font-size: var(--text-footnote); white-space: nowrap; }
	@container doc-header (max-width: 32.5rem) {
		/* Keep the approved compact row; a held sender may wrap rather than leave the canvas. */
		.document-tools { flex-wrap: nowrap; align-items: center; gap: 0; }
		.document-tools:has(:global(.sending-now)) { flex-wrap: wrap; max-inline-size: 100%; justify-content: flex-end; }
		/* A reason and its Retry take their own line, so the reason is whole and reachable instead of running off the left edge. */
		.document-tools:has(:global(.undelivered:not(.idle))) { flex-wrap: wrap; max-inline-size: 100%; justify-content: flex-end; }
	}
	@media (max-width: 768px) {
		@container doc-header (width < 408px) {
			/* A phone's 99 and 100+ counts, and a reviewer group that overflows into "+N", need the shorter visible preference label so Send stays whole in one row; the accessible name and 44px target stay unchanged. */
			:global(.document-tools:has(.sending-now [data-short='Send 99'])) .delivery-toggle,
			:global(.document-tools:has(.sending-now [data-short='Send 99+'])) .delivery-toggle,
			:global(.document-tools:has(.reviewer-group .more)) .delivery-toggle { gap: 0; }
			:global(.document-tools:has(.sending-now [data-short='Send 99'])) .delivery-toggle > span,
			:global(.document-tools:has(.sending-now [data-short='Send 99+'])) .delivery-toggle > span,
			:global(.document-tools:has(.reviewer-group .more)) .delivery-toggle > span { font-size: 0; }
			:global(.document-tools:has(.sending-now [data-short='Send 99'])) .delivery-toggle > span::after,
			:global(.document-tools:has(.sending-now [data-short='Send 99+'])) .delivery-toggle > span::after,
			:global(.document-tools:has(.reviewer-group .more)) .delivery-toggle > span::after { content: 'Deliver'; font-size: var(--text-footnote); }
		}
	}
	@container doc-canvas (max-width: 37.5rem) {
		.document-metadata:not(.expanded) { display: none; }
		.identity-details:has(.identity-age) { display: flex; }
		.identity-age { display: flex; width: 100%; gap: 6px; color: var(--text-secondary); font-size: var(--text-caption); container: identity-age / inline-size; }
		.identity-owner { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		.document-identity:has(.identity-owner:not(.has-freshness)) { flex-wrap: nowrap; }
		.identity-details:has(.identity-owner:not(.has-freshness)) { flex: 1 1 0; min-width: 6ch; }
		.document-identity:has(.identity-owner:not(.has-freshness)) .document-tools { flex: 0 1 auto; min-width: 0; }
		.document-identity:has(.identity-owner:not(.has-freshness)) .delivery-toggle { min-width: 0; flex: 0 1 auto; }
		.document-identity:has(.identity-owner:not(.has-freshness)) .delivery-toggle > span { min-width: 8ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		/* The verified phrase carries the picker when the owner cannot fit. */
		.identity-verified { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
		@container identity-age (width < 7.5rem) { .identity-owner.has-freshness { display: none; } }
		.document-title { display: none; }
		/* Keep the approved visible delivery label; compact tool padding yields before any label does. */
		.document-header :global(.doc-tool) { min-height: var(--touch-target); padding: 0 var(--space-2); }
		.delivery-toggle { min-height: var(--touch-target); }
		.delivery-toggle :global(.tg) { width: var(--touch-target); height: var(--touch-target); }
		/* The tool row cannot shrink, and with Reload, the count and the review controls the save pill pushes it past the left edge: the save state is transient and Reload is not, so the pill yields. */
		.stale-control ~ .save-ghost { display: none; }
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
	@container doc-header (width < 408px) {
		/* The compact toolbar remains one row even when the review surface occupies the header. */
		.document-header:has(:global(.rb-slot)) .document-tools { grid-column: 1 / -1; min-width: 0; justify-content: flex-end; }
		.document-header:has(:global(.rb-slot)) > :global(.rb-slot) { grid-row: 2 / 3; }
	}
	/* History is a text button on a wide canvas and, like the Request review button beside it (DocReviewControls.svelte:97), an icon-only button once the canvas is phone-sized, so the row holds the same controls whatever the save state. */
	.history-tool svg { display: none; width: 14px; height: 14px; fill: none; stroke: currentColor; stroke-width: 2.1; stroke-linecap: round; stroke-linejoin: round; flex: none; }
	@container doc-canvas (max-width: 600px) {
		.history-tool svg { display: block; }
		.history-tool .label { display: none; }
	}
	@media (max-width: 768px) {
		.delivery-toggle :global(.tg) { width: var(--touch-target); height: var(--touch-target); }
	}
	@media (any-pointer: coarse) {
		@container doc-canvas (max-width: 600px) {
			.document-header:has(:global(.rb-slot)) { align-items: center; }
			.document-header:has(:global(.rb-slot)) :global(.doc-tool) { min-height: var(--touch-target); }
		}
	}
</style>
