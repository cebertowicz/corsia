<script lang="ts">
	import { tick } from 'svelte';
	import { get } from 'svelte/store';
	import { Button } from './ui';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import type { CommentLoad } from '$lib/stores/docComments';
	import AdaptiveDrawer from './ui/AdaptiveDrawer.svelte';
	import Popover from './ui/Popover.svelte';
	import CommentComposer from './comments/CommentComposer.svelte';
	import CommentList from './comments/CommentList.svelte';
	import ImageRegionThumb from './ImageRegionThumb.svelte';
	import { decideSuggestion, SuggestionRefused, type SuggestionDecision } from '$lib/api/commentSuggestions';
	import { flushDraft, seedDraft } from '$lib/stores/docSaveQueue';
	import { loadComments, type DocComment } from '$lib/stores/docComments';
	import { imageAlt, imageGroupKey } from '$lib/docImageComments';
	import { suggestionRefusals, recordRefusal, clearRefusal, clearRefusals } from '$lib/stores/suggestionRefusals';
	import type { DocViewerTarget } from '$lib/docViewerTarget';
	import type { CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
	import type { DocFeedback } from '$lib/docFeedback.svelte';
	type SurfaceState = { kind: 'draft' } | { kind: 'thread'; id: string } | { kind: 'list' } | null;
	/**
	 * Comments on an image (DCOL-FR13.6): its thumbnail heads the list, cards carry their tag's number and colour.
	 * `hoveredId` and `onHover` link each card with its mark on the image; the drawer scrolls to the selected card itself.
	 */
	export type ImageCommentSurface = { src: string; alt?: string; colorOf: (comment: DocComment) => string; onMovePin?: (id: string) => void; onOpen?: () => void; showThumbnail?: boolean; imageBounds?: () => DOMRect | null; hoveredId?: string | null; onHover?: (id: string | null) => void; picks?: number };
	/**
	 * A document's images with comments on parts of them (DCOL-FR13.6): each image's comments sit together in the drawer
	 * under its thumbnail, and their cards carry their tag's number and colour. `src` is null for an image with no file.
	 */
	export type DocImageComments = { srcOf: (comment: DocComment) => string | null; colorOf: (comment: DocComment) => string; onOpen: (comment: DocComment, focus?: { id: string; move?: boolean }) => void };
	let { state: surfaceState, anchor, bounds = null, narrow, comments, focusedId, draft, draftReason = '', onDraftBody, onCancelDraft, onClose, restoreFocus, onFocus, onSubmit, onReply, onResolve, onAgree, onViewed, target, artifactId, readOnly = false, onSuggestionBusy, onSuggestionAccepted, onDocumentChanged, load, onRetry, image = null, images = null, feedback = null }: {
		state: SurfaceState;
		anchor: CommentAnchor | null;
		/** The canvas the card stays inside. */
		bounds?: HTMLElement | null;
		narrow: boolean;
		comments: DocComment[];
		focusedId: string | null;
		/** `label` names what a non-text comment is on, in place of the quote. */
		draft: { text: string; body?: string; label?: string } | null;
		draftReason?: string;
		onDraftBody?: (text: string) => void;
		onCancelDraft?: () => void;
		onClose: () => void;
		restoreFocus: () => void;
		onFocus: (id: string) => void;
		onSubmit: (text: string) => Promise<void>;
		onReply: (id: string, text: string) => Promise<void>;
		onResolve: (id: string, resolved: boolean) => void;
		onAgree: (id: string, agreed: boolean) => void;
		onViewed?: (id: string, replyId: string) => Promise<void>;
		target: DocViewerTarget | null;
		artifactId: string | null;
		readOnly?: boolean;
		/** A decision is in flight (`busy`): the editor holds still, and while it is an Accept the suggestion `id` is marked busy. */
		onSuggestionBusy?: (busy: boolean, id?: string) => void;
		onSuggestionAccepted?: (text: string) => void;
		/** The server applied an accepted edit but could not record the decision: read the shown document again. */
		onDocumentChanged?: () => void;
		load?: CommentLoad;
		onRetry?: () => void;
		image?: ImageCommentSurface | null;
		images?: DocImageComments | null;
		/** The open document's batch: while it holds messages not sent yet the drawer ends in their Send now footer. */
		feedback?: DocFeedback | null;
	} = $props();
	const asDoc = (comment: unknown) => comment as DocComment;
	/** A comment still on its image in the document; one whose image markdown is gone has nowhere to open. */
	const onItsImage = (comment: DocComment) => !comment.orphaned && comment.from != null;
	function movePinInDocument(id: string) {
		const comment = comments.find((item) => item.id === id);
		if (comment && onItsImage(comment)) images?.onOpen(comment, { id, move: true });
	}
	let filter = $state<'all' | 'open' | 'resolved'>('all');
	let drawer = $state<HTMLElement>();
	// A boolean, so effects that branch on it do not re-run each time the image props object is rebuilt (every hover).
	const isImage = $derived(!!image);
	const picks = $derived(image?.picks ?? 0);
	let navigationNotice = $state('');
	const byNumber = (a: DocComment, b: DocComment) => (a.regionNumber ?? Infinity) - (b.regionNumber ?? Infinity);
	const displayed = $derived.by(() => {
		if (surfaceState?.kind === 'thread') return comments.filter((comment) => comment.id === surfaceState.id);
		const shown = comments.filter((comment) => filter === 'all' || (filter === 'resolved' ? comment.resolved : !comment.resolved));
		// An image's comments read in number order, like its tags.
		return image ? [...shown].sort(byNumber) : shown;
	});
	const followed = $derived(surfaceState?.kind === 'thread' ? comments.find((item) => item.id === surfaceState.id) : undefined);
	// Leaving the document sends the batch (DocFeedback.leave); in its grace period it says when it goes instead.
	const unsentText = $derived.by(() => {
		if (!feedback?.pendingCount) return '';
		const count = `${feedback.pendingCount} message${feedback.pendingCount === 1 ? '' : 's'} not sent yet`;
		const owner = feedback.ownerName ?? 'the owner';
		if (feedback.sendingGrace) return `${count} · sending to ${owner} in ${feedback.graceMinutes} min`;
		return `${count} · ${feedback.pendingCount === 1 ? 'it goes' : 'they go'} to ${owner} when you leave.`;
	});
	/** The grid owns the drawer's maximum; add this measured pinned footer before that cap so the list keeps its normal room when it can. */
	function drawerFooterHeight(node: HTMLElement) {
		const doc = node.closest<HTMLElement>('.doc');
		const update = () => doc?.style.setProperty('--doc-drawer-footer-height', `${node.getBoundingClientRect().height}px`);
		update();
		const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(update);
		observer?.observe(node);
		return { destroy() { observer?.disconnect(); doc?.style.removeProperty('--doc-drawer-footer-height'); } };
	}
	// A card that follows its text closes once its comment is deleted: there is nothing left to show.
	$effect(() => {
		if (surfaceState?.kind !== 'thread' || typeof anchor !== 'function' || load?.state === 'loading') return;
		if (!followed) onClose();
	});
	// Where the followed card last sat. A comment that is resolved or has lost its text has no place in the text any more, but its
	// card stays open there: closing it would take the refusal reason and any reply being typed with it.
	let lastRect: { id: string; rect: DOMRect } | null = null;
	const cardAnchor = $derived.by((): CommentAnchor | null => {
		const follow = anchor, id = surfaceState?.kind === 'thread' ? surfaceState.id : null;
		if (typeof follow !== 'function' || id === null) return anchor;
		const placeless = !!followed && (followed.resolved || !!followed.orphaned);
		return () => {
			const rect = placeless && lastRect?.id === id ? lastRect.rect : follow();
			if (rect) lastRect = { id, rect };
			return rect;
		};
	});
	$effect(() => {
		if (surfaceState?.kind !== 'list') return;
		filter = 'all'; navigationNotice = '';
		void tick().then(() => {
			const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
			// An image's drawer opens at the selected comment instead (the effect below).
			if (list && !(isImage && focusedId)) list.scrollTop = 0;
			drawer?.querySelector<HTMLElement>('[data-drawer-filter]')?.focus();
		});
	});
	/** Bring a card into view in the drawer's own list: smooth, or at once when asked or with reduced motion. */
	function scrollToCard(id: string, instant: boolean) {
		const list = drawer?.querySelector<HTMLElement>('.drawer-comments');
		const card = list?.querySelector<HTMLElement>(`[data-comment-id="${id}"]`);
		if (!list || !card) return;
		const top = card.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop - 4;
		list.scrollTo({ top: Math.max(0, top), behavior: instant || get(prefersReducedMotion) ? 'instant' : 'smooth' });
	}
	// An image's selected comment, picked on the image or there when the drawer opens, is scrolled to and marked in it.
	// The drawer opens already at its card, so only the drawer moves; a pick while it is open glides the list.
	let listShown = false;
	$effect(() => {
		const id = focusedId;
		// Picking the selected tag again scrolls back to its card.
		void picks;
		const shown = isImage && surfaceState?.kind === 'list';
		const opening = shown && !listShown;
		listShown = shown;
		if (!shown || !id) return;
		void tick().then(() => scrollToCard(id, opening));
	});
	/**
	 * An image's drawer slides with the grid track its viewer animates, so it stays mounted while the track closes; with
	 * reduced motion it crossfades instead. A document's drawer keeps appearing and leaving at once.
	 */
	function drawerMotion(_node: Element, { leaving }: { leaving: boolean }) {
		if (!isImage) return { duration: 0 };
		const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
		const pace = leaving ? 0.7 : 1;
		if (get(prefersReducedMotion)) return { duration: token('--dur-tight') * pace, css: (t: number) => `opacity: ${t}` };
		return { duration: leaving ? token('--dur-drawer') * pace : 0 };
	}
	function closeDrawer() { onClose(); restoreFocus(); }
	function navigate(id: string) {
		const comment = comments.find((item) => item.id === id);
		navigationNotice = comment?.orphaned || comment?.review ? 'This comment has no text to jump to. Its quote and discussion remain here.' : '';
		onFocus(id);
	}
	function onDrawerKey(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		closeDrawer();
	}
	function drawerKeys(element: HTMLElement) {
		element.addEventListener('keydown', onDrawerKey);
		return { destroy() { element.removeEventListener('keydown', onDrawerKey); } };
	}
	const authorsWithOpenSuggestions = $derived.by(() => Object.entries(
		comments.filter((comment) => !comment.resolved && !comment.orphaned && comment.replacement !== null && comment.replacement !== undefined)
			.reduce<Record<string, DocComment[]>>((groups, comment) => ({ ...groups, [comment.author]: [...(groups[comment.author] ?? []), comment] }), {})
	).filter(([, suggestions]) => suggestions.length >= 2));
	// Whose accept-all is running. It disables every accept-all on purpose, across documents: one request at a time.
	let acceptAllBusy = $state<string | null>(null);
	const surfaceOpen = $derived(surfaceState !== null);
	// What one opening of the surface shows from its decisions: each card's failure (the surface makes the request, and a
	// card that moves groups remounts and would lose its own), the accept-all error, the skipped list and whose accept-all
	// it shows as running. A new document or opening replaces it; a decision writes only through `settle`, to the outcome
	// it started with, so nothing of it lands on what replaced it.
	type Outcome = { errors: Record<string, string>; acceptAllError: string; skipped: { id: string; reason: string }[]; acceptAllShownBusy: string | null };
	const fresh = (): Outcome => ({ errors: {}, acceptAllError: '', skipped: [], acceptAllShownBusy: null });
	let outcome = $state<Outcome>(fresh());
	function settle(started: Outcome, change: (current: Outcome) => void) { if (started === outcome) change(outcome); }
	// A refused accept (kind `stale`) goes to `suggestionRefusals` instead, which also shows its card as orphaned.
	const cardErrors = $derived({
		...(artifactId ? $suggestionRefusals[artifactId] : undefined),
		...Object.fromEntries(Object.entries(outcome.errors).filter(([, message]) => message)),
	});
	// The reset ends this document's refusals too: reopening the comments offers Accept again.
	$effect(() => {
		const artifact = artifactId; surfaceOpen;
		outcome = fresh();
		return () => { if (artifact) clearRefusals(artifact); };
	});
	const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));
	async function submitSuggestionDecision(id: string, decision: 'accept' | 'reject', allFromAuthor = false) {
		// The document this decision is for. The props may name another one by the time it settles.
		const started = outcome, doc = target, artifact = artifactId;
		onSuggestionBusy?.(true, decision === 'accept' ? id : undefined);
		try {
			if (decision === 'accept') {
				if (!doc || !await flushDraft(doc)) throw new Error("Couldn't save the document before accepting this suggestion");
			}
			const result: SuggestionDecision = allFromAuthor
				? await decideSuggestion(id, decision, true)
				: await decideSuggestion(id, decision);
			settle(started, (current) => { current.skipped = result.skipped ?? []; });
			if (decision === 'accept' && result.text !== undefined && result.revision !== undefined && doc) {
				// The accepted text goes on screen only while that document's tab is still the one shown; closing the surface does not change that.
				const shown = target?.instanceId === doc.instanceId ? target : null;
				seedDraft(shown ?? doc, result.text, result.revision);
				if (shown) onSuggestionAccepted?.(result.text);
			}
			if (artifact) await loadComments(artifact);
		} catch (error) {
			// Bound to the document, like the accepted text. A decision made elsewhere comes back with a comments load; an
			// edit applied but not recorded is in the saved file, so the shown document is read again with its comments.
			const kind = error instanceof SuggestionRefused ? error.kind : null;
			if (kind === 'unrecorded' && doc && target?.instanceId === doc.instanceId) onDocumentChanged?.();
			else if ((kind === 'decided' || kind === 'unrecorded') && artifact) void loadComments(artifact).catch(() => undefined);
			throw error;
		} finally {
			onSuggestionBusy?.(false);
		}
	}
	async function decideOne(id: string, decision: 'accept' | 'reject') {
		const started = outcome, artifact = artifactId;
		started.errors = { ...started.errors, [id]: '' };
		try {
			await submitSuggestionDecision(id, decision);
			if (artifact) clearRefusal(artifact, id);
		} catch (error) {
			// The saved file no longer has the quote: the card stays orphaned with this reason until it is decided or the surface resets.
			const stale = decision === 'accept' && error instanceof SuggestionRefused && error.kind === 'stale';
			settle(started, (current) => {
				if (stale && artifact) recordRefusal(artifact, id, reason(error));
				else current.errors = { ...current.errors, [id]: reason(error) };
			});
		}
	}
	async function acceptAllFrom(author: string, id: string) {
		if (acceptAllBusy !== null) return;
		const started = outcome;
		acceptAllBusy = author; started.acceptAllShownBusy = author; started.acceptAllError = '';
		try {
			await submitSuggestionDecision(id, 'accept', true);
		} catch (error) {
			settle(started, (current) => { current.acceptAllError = reason(error); });
		} finally {
			acceptAllBusy = null;
			settle(started, (current) => { current.acceptAllShownBusy = null; });
		}
	}
</script>

{#snippet imageHead(row: unknown[])}
	{@const group = row as DocComment[]}
	{@const src = images?.srcOf(group[0])}
	{#if images && src}<ImageRegionThumb {src} alt={imageAlt(group[0])} caption comments={group} colorOf={images.colorOf} onOpen={onItsImage(group[0]) ? () => images?.onOpen(group[0]) : undefined} />{/if}
{/snippet}

{#snippet content()}
	{#if surfaceState?.kind === 'draft' && draft}<CommentComposer quote={draft.text} label={draft.label} body={draft.body ?? ''} disabledReason={draftReason} onBody={onDraftBody} onSubmit={onSubmit} onCancel={onCancelDraft ?? onClose} />
	{:else}
		{#if !readOnly && surfaceState?.kind !== 'thread'}
			<div class="suggestion-status">
				{#each authorsWithOpenSuggestions as [author, suggestions] (author)}
					<button type="button" aria-busy={outcome.acceptAllShownBusy === author} disabled={acceptAllBusy !== null} onclick={() => acceptAllFrom(author, suggestions[0].id)}>{outcome.acceptAllShownBusy === author ? 'Accepting…' : `Accept all from ${author}`}</button>
				{/each}
				{#if outcome.skipped.length}<details><summary>{outcome.skipped.length} skipped</summary>{#each outcome.skipped as item (item.id)}<p>{item.reason}</p>{/each}</details>{/if}
			</div>
			{#if outcome.acceptAllError}<div class="suggestion-error" role="alert">{outcome.acceptAllError}</div>{/if}
		{/if}
		<CommentList comments={displayed} documentOrder={surfaceState?.kind === 'list'} {focusedId} hoveredId={image?.hoveredId ?? null} onHover={image?.onHover} scrollOnFocus={!isImage} {onViewed} onFocus={surfaceState?.kind === 'list' ? navigate : onFocus} {onReply} {onResolve} {onAgree} onDecision={decideOne} decisionErrors={cardErrors} tagColor={image?.colorOf ?? (images ? (comment) => images.colorOf(asDoc(comment)) : undefined)} onMovePin={image?.onMovePin ?? (images ? movePinInDocument : undefined)} groupOf={images ? (comment) => imageGroupKey(asDoc(comment)) : undefined} groupHead={images ? imageHead : undefined} {readOnly} reopenReachesOwner={load?.state === 'ready' && load.owner != null} />
	{/if}
{/snippet}

{#if surfaceState}
	{#if surfaceState.kind === 'list'}
		<section class="canvas-comment-drawer" data-doc-comments-drawer aria-label="Document comments" bind:this={drawer} use:drawerKeys in:drawerMotion|global={{ leaving: false }} out:drawerMotion|global={{ leaving: true }}>
			<div class="drawer-heading"><h2>Comments <span>{comments.length}</span></h2><Button size="sm" variant="ghost" onclick={closeDrawer}>Done</Button></div>
			<div class="comment-filters" role="group" aria-label="Filter comments">
				{#each ['all', 'open', 'resolved'] as choice}
					<button type="button" data-drawer-filter aria-pressed={filter === choice} onclick={() => filter = choice as typeof filter}>{choice[0].toUpperCase() + choice.slice(1)}</button>
				{/each}
			</div>
			{#if load?.state === 'loading'}<p class="drawer-status">Loading comments…</p>{/if}
			{#if load?.state === 'error'}<div class="drawer-status" role="status">Couldn't load comments. {load.error}<Button size="sm" onclick={onRetry}>Retry</Button></div>{/if}
			{#if navigationNotice}<p class="drawer-status">{navigationNotice}</p>{/if}
			<div class="drawer-comments">
				{#if image && image.showThumbnail !== false && displayed.some((comment) => comment.region)}<ImageRegionThumb src={image.src} alt={image.alt} comments={displayed} colorOf={image.colorOf} onOpen={image.onOpen} />{/if}
				{#if displayed.length}{@render content()}
				{:else if load?.state !== 'loading' && load?.state !== 'error'}<p class="drawer-status">{comments.length === 0 ? 'No comments yet.' : filter === 'open' ? 'No open comments.' : 'No resolved comments.'}</p>{/if}
			</div>
			{#if unsentText}
				<div class="drawer-unsent" data-drawer-unsent use:drawerFooterHeight>
					<p role="status">{unsentText}</p>
					<Button size="sm" onclick={() => void feedback?.send()} loading={feedback?.sending}>Send now</Button>
				</div>
			{/if}
		</section>
	{:else if narrow}<AdaptiveDrawer ariaLabel="Document comments" heading={surfaceState.kind === 'draft' ? 'New comment' : 'Comments'} followKeyboard {onClose}>{@render content()}</AdaptiveDrawer>
	{:else}<Popover anchor={cardAnchor} {bounds} avoidRect={image?.imageBounds} initialFocus={image && !readOnly ? '.ct-input' : undefined} open={true} {onClose} {restoreFocus}>{@render content()}</Popover>{/if}
{/if}

<style>
	.canvas-comment-drawer { grid-area: drawer; display: flex; flex-direction: column; gap: 4px; min-height: 0; min-width: 0; position: relative; isolation: isolate; padding: 6px 8px 8px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); }
	/* The glass material is its own layer under the content, so the drawer's own box stays plain for the contrast probe, which cannot composite a backdrop filter. */
	.canvas-comment-drawer::before { content: ''; position: absolute; inset: 0; z-index: -1; border-radius: inherit; background: var(--glass-regular-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); box-shadow: var(--glass-rim), var(--shadow-chip); pointer-events: none; }
	.drawer-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding-left: 4px; }
	.drawer-heading h2 { margin: 0; font-size: var(--text-subhead); font-weight: var(--fw-semibold); color: var(--text-secondary); }
	.drawer-heading h2 span { color: var(--text-tertiary); font-size: var(--text-caption); font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; margin-left: 2px; }
	/* A segmented control: one track, the chosen segment raised on it. */
	.comment-filters { display: flex; gap: 2px; flex: none; padding: 2px; border-radius: var(--radius-xs); background: color-mix(in srgb, var(--text-primary) 6%, transparent); }
	.comment-filters button { position: relative; flex: 1; height: 22px; border: 0; border-radius: var(--radius-2xs); color: var(--text-secondary); background: none; font: inherit; font-size: var(--text-footnote); font-weight: var(--fw-medium); cursor: pointer; transition: background var(--dur-tight), color var(--dur-tight); }
	.comment-filters button:hover { color: var(--text-primary); }
	.comment-filters button[aria-pressed="true"] { color: var(--text-primary); font-weight: var(--fw-semibold); background: var(--glass-thick); box-shadow: var(--glass-rim), 0 1px 2px rgba(0, 0, 0, 0.12); }
	:global([data-theme="dark"]) .comment-filters button[aria-pressed="true"] { background: color-mix(in srgb, var(--text-primary) 16%, transparent); }
	.comment-filters button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
	@media (pointer: coarse) { .drawer-heading :global(button), .comment-filters button { min-height: 44px; } }
	.drawer-comments { flex: 1; min-height: 0; overflow: auto; overscroll-behavior: contain; scrollbar-gutter: stable; }
	.drawer-status { color: var(--text-secondary); font-size: var(--text-footnote); margin: 4px 0; }
	/* Pinned under the list, edge to edge over the drawer's padding, opaque so cards never show through it. */
	.drawer-unsent { flex: none; display: flex; align-items: center; gap: var(--space-2); margin: 0 calc(-1 * var(--space-2)) calc(-1 * var(--space-2)); padding: var(--space-2) var(--space-3); border-top: 1px solid var(--border-subtle); border-radius: 0 0 calc(var(--radius-lg) - 1px) calc(var(--radius-lg) - 1px); background: var(--bg-secondary); }
	.drawer-unsent p { flex: 1; min-width: 0; margin: 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.drawer-unsent :global(.btn) { flex: none; }
	@media (pointer: coarse) { .drawer-unsent :global(.btn) { min-height: 44px; } }
	.suggestion-status { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
	.suggestion-status button { background: none; border: none; padding: 0; font: inherit; font-size: var(--text-caption); font-weight: var(--fw-semibold); color: var(--accent); cursor: pointer; }
	.suggestion-status button:not(:disabled):hover { text-decoration: underline; }
	.suggestion-status button:disabled { opacity: 0.55; cursor: not-allowed; }
	.suggestion-status button:disabled[aria-busy="true"] { color: var(--text-primary); background: var(--accent-bg); border-radius: var(--radius-xs); padding: 2px 6px; margin: -2px -6px; opacity: 1; }
	.suggestion-error { font-size: var(--text-caption); color: var(--red-text); }
	.suggestion-status details { color: var(--text-tertiary); font-size: var(--text-footnote); }
	.suggestion-status p { margin: 4px 0; }
</style>
