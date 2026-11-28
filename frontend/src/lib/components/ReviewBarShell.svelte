<script lang="ts" module>
	/** Context key: a surface that sets it true keeps the asker's name on a docked card's resting line (Activity). */
	export const ASKER_AT_REST = Symbol('askerAtRest');
</script>

<script lang="ts">
	import { getContext, tick, type Snippet } from 'svelte';
	import { get } from 'svelte/store';
	import { cubicIn } from 'svelte/easing';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { Avatar } from '$lib/components/ui';
	import MiddleName from './MiddleName.svelte';
	import { agentColor } from '$lib/docPresence';

	/**
	 * The ask shell every in-view ask renders through (the review ask and the person step): who asks on line 1,
	 * the ask below it, then a status and the answer, and the receipt that replaces it in place. The owner keeps
	 * what the ask says and how an answer is sent; `actions` is its answer controls (a note field may precede them).
	 */
	type Receipt = { icon: 'check' | 'changes' | 'dash'; label: string; segments: string[] };
	let {
		view, who, plus = 0, ask = '', lineLabel = '', full = '', others = [], askLabel, noting = false, busy = false,
		status = '', suppressed = false, docked = false, emailReview = false, capsuleClearance = $bindable(0), receipt, stale, actions,
	}: {
		/** The ask answers from a card docked to the document canvas's bottom edge: at rest the question on one line above the answers; a tap opens it to the asker and the full question. The caller docks only on a phone or a short touch screen; elsewhere the ask keeps its row. */
		docked?: boolean;
		emailReview?: boolean;
		view: 'ask' | 'receipt' | 'stale';
		who?: { name: string; id: string };
		/** How many others also asked. */
		plus?: number;
		/** The one-line ask beside the name; with it, line 1 expands to `full`. Without it, `full` always shows. */
		ask?: string;
		lineLabel?: string;
		full?: string;
		others?: readonly { name: string; reason: string }[];
		askLabel: string;
		noting?: boolean;
		busy?: boolean;
		status?: string;
		suppressed?: boolean;
		/** Out: px the bar's container leaves at its bottom so the floating capsule never covers it; 0 while it does not float. */
		capsuleClearance?: number;
		receipt?: Receipt | null;
		stale?: { label: string; text: string; sub: string };
		actions: Snippet;
	} = $props();

	const askerAtRest = getContext<boolean | undefined>(ASKER_AT_REST) === true;
	const expandable = $derived(ask !== '');
	let expanded = $state(false);
	/** The docked card, opened by a tap on its question. A new ask starts it closed. */
	let cardOpen = $state(false);
	$effect(() => {
		if (view !== 'ask') cardOpen = false;
	});
	let receiptEl = $state<HTMLElement>();
	let slotEl = $state<HTMLElement>();
	let capsuleEl = $state<HTMLElement>();
	let askedHere = false;
	// The receipt takes focus when an answer given here replaces the ask, never when a reload opens on it.
	$effect(() => {
		if (view === 'ask') askedHere = true;
		else if (view === 'receipt' && askedHere) void tick().then(() => receiptEl?.focus());
	});

	const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
	/** A new state replaces the old in place: the old row fades out as the new fades in, stacked in one grid cell, so the slot keeps its height (motion.md, content swaps in place). `content` fades only the ask's in-flow text, never the floating capsule, which has its own slide. */
	function swap(node: Element, { leaving, content = false }: { leaving: boolean; content?: boolean }) {
		const duration = token('--dur-tight') * (leaving ? 0.7 : 1);
		if (content) return { duration, tick: (t: number) => (node as HTMLElement).style.setProperty('--rb-fade', String(t)) };
		return { duration, css: (t: number) => `opacity: ${t}` };
	}
	/** The capsule leaves by sliding back below the screen edge, opaque all the way; its entrance is the CSS keyframes below. It slides only when an answer ends the ask: a global transition keeps the whole viewer in the DOM until it ends, so a bar torn down with its viewer leaves at once. */
	function capsuleOut(node: Element) {
		const capsule = node as HTMLElement;
		if (get(prefersReducedMotion) || view === 'ask' || getComputedStyle(capsule).position !== 'fixed') return { duration: 0 };
		const distance = window.innerHeight - capsule.getBoundingClientRect().top;
		return { duration: token('--dur-standard') * 0.7, easing: cubicIn, css: (_t: number, u: number) => `transform: translateY(${u * distance}px)` };
	}
	/**
	 * How far the floating capsule (a phone-width touch screen at a narrow canvas) reaches up into the bar's container (its offset
	 * parent, else the window), plus the capsule's side inset as the gap above it. Zero while the capsule sits in the
	 * bar's own row, is tucked away, or is not there.
	 */
	function measureClearance(): number {
		const capsule = capsuleEl;
		if (!capsule || suppressed) return 0;
		// The docked card reaches up from the canvas's bottom edge into the box it is placed against; the document keeps its last lines clear of it.
		if (docked) {
			const box = capsule.offsetParent?.getBoundingClientRect();
			const reach = box ? box.bottom - capsule.getBoundingClientRect().top : 0;
			return reach > 0 ? Math.round(reach) : 0;
		}
		const style = getComputedStyle(capsule);
		if (style.position !== 'fixed') return 0;
		// From its layout box, not its rect: the capsule slides in by a transform, which would move the measured top.
		const top = window.innerHeight - (Number.parseFloat(style.bottom) || 0) - capsule.offsetHeight;
		const container = slotEl?.offsetParent;
		const reach = (container ? container.getBoundingClientRect().bottom : window.innerHeight) - top;
		return reach > 0 ? Math.round(reach + (Number.parseFloat(style.left) || 0)) : 0;
	}
	// The capsule's own size (it grows with a status or the note) and the window's decide whether and how far it
	// floats over the container; its tucked-away state is the `suppressed` prop, read by the measure.
	$effect(() => {
		const capsule = capsuleEl;
		const update = () => { capsuleClearance = measureClearance(); };
		update();
		if (!capsule) return;
		const sizes = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null;
		sizes?.observe(capsule);
		window.addEventListener('resize', update);
		return () => {
			sizes?.disconnect();
			window.removeEventListener('resize', update);
		};
	});
</script>

{#if docked}
<!-- Docked to the document canvas's left, right and bottom edges. A review ask cuts one thing per line: at rest the question alone, then the answers; opened, the asker takes its own line above the full question. -->
<div class="rb-dock-slot" bind:this={slotEl}>
{#if view === 'ask' && who}
	<section class="rb-dock" class:suppressed class:noting class:asker={askerAtRest} class:open={cardOpen || noting} data-ask-card aria-label={askLabel} aria-busy={busy} bind:this={capsuleEl} in:swap={{ leaving: false }}>
		<button type="button" class="rb-dock-ask" title={full || ask} aria-expanded={cardOpen} aria-label={lineLabel || `${who.name}${plus ? ` +${plus}` : ''}: ${full || ask}`} onclick={() => (cardOpen = !cardOpen)}>
			<span class="rb-dock-text"><span class="rb-dock-by"><strong class="rb-dock-who">{#if cardOpen}{who.name}{:else}<MiddleName name={who.name} />{/if}</strong>{#if plus}<span class="rb-plus">+{plus}</span>{/if}</span><span class="rb-dock-sep" aria-hidden="true">·</span><span class="rb-dock-q">{full || ask}</span></span>
		</button>
		{#if (cardOpen || noting) && others.length}
			<ul class="rb-others">
				{#each others as other (other.name)}<li><strong>{other.name}</strong>: {other.reason}</li>{/each}
			</ul>
		{/if}
		<span class="rb-status" class:busy role="status">{status}</span>
		{@render actions()}
	</section>
{:else if view === 'receipt' && receipt}
	<section class="rb-dock receipt" class:changes={receipt.icon === 'changes'} data-ask-card aria-label={receipt.label} bind:this={capsuleEl} in:swap={{ leaving: false }}>
		<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16">
			{#if receipt.icon === 'changes'}<g transform="scale(0.666667)"><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></g>
			{:else if receipt.icon === 'dash'}<circle cx="8" cy="8" r="6" /><path d="M5 8h6" />
			{:else}<circle cx="8" cy="8" r="6" /><path d="M5.3 8.2l1.9 1.9 3.5-3.9" />{/if}
		</svg>
		<span class="rb-receipt" role="status" tabindex="-1" bind:this={receiptEl}><span class="rb-text">{receipt.segments[0] ?? ''}</span>{#each receipt.segments.slice(1) as segment (segment)}<span class="rb-seg"> · {segment}</span>{/each}</span>
	</section>
{:else if view === 'stale' && stale}
	<section class="rb-dock stale" role="status" aria-label={stale.label} bind:this={capsuleEl} in:swap={{ leaving: false }}>
		<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M13 8a5 5 0 11-1.5-3.6M13 2.5v3h-3" /></svg>
		<span class="rb-text">{stale.text}</span><span class="rb-sub">{stale.sub}</span>
	</section>
{/if}
</div>
{:else}
<!-- The slot outlives the phase: it grows once when the first ask arrives, and an answer or a replaced notice swaps inside it at the same height. -->
<div class="rb-slot" bind:this={slotEl}>
{#if view === 'receipt' && receipt}
	<section class="review-bar receipt" class:changes={receipt.icon === 'changes'} aria-label={receipt.label} in:swap={{ leaving: false }} out:swap={{ leaving: true }}>
		<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16">
			{#if receipt.icon === 'changes'}<g transform="scale(0.666667)"><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></g>
			{:else if receipt.icon === 'dash'}<circle cx="8" cy="8" r="6" /><path d="M5 8h6" />
			{:else}<circle cx="8" cy="8" r="6" /><path d="M5.3 8.2l1.9 1.9 3.5-3.9" />{/if}
		</svg>
		<span class="rb-receipt" role="status" tabindex="-1" bind:this={receiptEl}>
			<!-- One flow: the verdict and then each segment, wrapping by whole segments. A separator leads the segment it introduces, so no line ends on one. The row is pulled left by the width of one separator inside a box that clips its inline start, so whichever separator lands at the start of a line, after a wrap, sits outside the box and is not drawn; the verdict has none and is moved right by the same width. -->
			<span class="rb-rest"><span class="rb-rest-row">{#each receipt.segments as segment, index (segment)}<span class="rb-seg" class:rb-first={index === 0}>{#if index === 0}<span class="rb-text">{segment}</span>{:else}<span class="rb-dot" aria-hidden="true">·&nbsp;</span>{segment}{/if}</span>{' '}{/each}</span></span>
		</span>
	</section>
{:else if view === 'stale' && stale}
	<section class="review-bar stale" role="status" aria-label={stale.label} in:swap={{ leaving: false }} out:swap={{ leaving: true }}>
		<svg class="rb-icon" aria-hidden="true" viewBox="0 0 16 16"><path d="M13 8a5 5 0 11-1.5-3.6M13 2.5v3h-3" /></svg>
		<span class="rb-text">{stale.text}</span><span class="rb-sub">{stale.sub}</span>
	</section>
{:else if view === 'ask' && who}
	<section class="review-bar ask" class:email-review={emailReview} class:suppressed class:noting class:expanded class:static={!expandable} aria-label={askLabel} aria-busy={busy} in:swap={{ leaving: false, content: true }} out:swap={{ leaving: true, content: true }}>
		<span class="rb-head">
			{#snippet lead()}
				<span class="rb-lead">
					<Avatar name={who.name} color={agentColor(who.id)} hidden />
					<span class="rb-who"><strong>{#if emailReview}{who.name}{:else}<MiddleName name={who.name} />{/if}</strong>{#if plus}<span class="rb-plus" aria-hidden="true">+{plus}</span>{/if}:</span>
				</span>
			{/snippet}
			{#if expandable}
				<button type="button" class="rb-line" aria-expanded={expanded} aria-label={lineLabel} title={ask} onclick={() => (expanded = !expanded)}>
					{@render lead()}
					<span class="rb-ask">{ask}</span>
					<svg class="rb-icon rb-chev" aria-hidden="true" viewBox="0 0 16 16"><path d="M4 6.5l4 4 4-4" /></svg>
				</button>
			{:else}
				<span class="rb-line">{@render lead()}</span>
			{/if}
			{#if expanded || !expandable}<p class="rb-ask-full">{full}</p>{/if}
			{#if expanded && others.length}
				<ul class="rb-others">
					{#each others as other (other.name)}<li><strong>{other.name}</strong>: {other.reason}</li>{/each}
				</ul>
			{/if}
		</span>
		<span class="rb-row2">
			<div class="rb-capsule" class:suppressed class:noting bind:this={capsuleEl} out:capsuleOut|global>
				<!-- At a fine pointer the status keeps its height when empty, so the buttons never move when a status appears. On a phone it shows above the buttons inside the floating capsule, which grows upward. -->
				<span class="rb-status" class:busy={busy} role="status">{status}</span>
				{@render actions()}
			</div>
		</span>
	</section>
{/if}
</div>
{/if}

<style>
	/* The docked card: placed against the document box, which sits inside the canvas's padding, so it reaches past that padding to the canvas's edges; its content keeps the document's inset. Solid, not glass: document text would read through. */
	.rb-dock-slot { display: contents; }
	.rb-dock { position: absolute; z-index: 5; left: calc(-1 * var(--canvas-pad-inline, 0px)); right: calc(-1 * var(--canvas-pad-inline, 0px)); bottom: calc(-1 * var(--canvas-pad-bottom, 0px)); box-sizing: border-box; display: flex; flex-wrap: wrap; align-items: center; column-gap: var(--space-4); row-gap: var(--space-1); padding: var(--space-3) calc(var(--space-3) + var(--canvas-pad-inline, 0px)) calc(var(--space-2) + var(--safe-bottom, 0px)); border: 1px solid var(--border-subtle); border-bottom: 0; border-radius: var(--radius-xl) var(--radius-xl) 0 0; background: var(--bg-secondary); box-shadow: var(--glass-elevation-1); font-size: var(--text-footnote); color: var(--text-secondary); }
	/* Hidden under a comment sheet, and while the Details panel would reach down over it (Details then carries the ask). */
	.rb-dock.suppressed, :global(.ask-covered) .rb-dock, :global(.ask-covered) .review-bar.ask { visibility: hidden; pointer-events: none; }
	.rb-dock :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; transition: none; }
	.rb-dock-ask { position: relative; flex: 1 0 100%; min-width: 0; padding: 0; border: 0; border-radius: var(--radius-sm); background: none; color: var(--text-primary); font: inherit; font-size: var(--text-subhead); line-height: var(--lh-normal); text-align: left; cursor: pointer; }
	/* At rest the question is one line; its tap area reaches up through the card's top padding and border and 8px down, so it is 44px without making the card taller. The answers sit above it, so they keep their own 44px. */
	.rb-dock-ask::after { content: ''; position: absolute; inset: calc(-1 * var(--space-3) - 1px) 0 calc(-1 * var(--space-2)) 0; }
	.rb-dock-ask:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.rb-dock-text { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 1; line-clamp: 1; overflow: hidden; overflow-wrap: break-word; }
	.rb-dock.open .rb-dock-text { -webkit-line-clamp: unset; line-clamp: none; }
	/* The name stays on one line, cut in the middle (MiddleName). */
	.rb-dock-who { display: inline-grid; max-width: 60%; vertical-align: bottom; font-weight: var(--fw-semibold); }
	.rb-dock-sep { margin-inline: var(--space-1); color: var(--text-tertiary); }
	.rb-dock .rb-dock-sep, .rb-dock:not(.open) .rb-dock-by { display: none; }
	.rb-dock.open .rb-dock-by { display: flex; min-width: 0; white-space: normal; }
	/* Activity: at rest the asker leads the question on its one line. */
	.rb-dock.asker:not(.open) .rb-dock-by, .rb-dock.asker:not(.open) .rb-dock-sep { display: inline; }
	.rb-dock.open .rb-dock-who { display: block; max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
	.rb-dock .rb-others { flex: 1 0 100%; padding-left: 0; }
	.rb-dock .rb-status { flex: 1 0 100%; }
	.rb-dock .rb-status:empty { display: none; }
	/* What Approve also does: left-aligned under the question and never cut. Beside the answers while its whole text fits there, else on its own line above them. */
	.rb-dock :global(.rb-derived) { flex: 1 1 auto; width: max-content; max-width: 100%; min-width: 0; margin: 0; }
	.rb-dock.open :global(.rb-derived) { flex-basis: 100%; white-space: normal; }
	.rb-dock :global(.rb-actions) { position: relative; z-index: 1; display: inline-flex; align-items: center; justify-content: flex-end; gap: var(--space-4); flex: 0 0 auto; margin-left: auto; }
	.rb-dock.noting :global(.rb-note) { flex: 1 0 100%; height: auto; min-height: var(--space-6); padding: var(--space-1) var(--space-2); line-height: var(--lh-normal); }
	/* Touch: 44px answers, and the question's tap area (8px below its line) keeps 8px clear of the answers' row. */
	@media (any-pointer: coarse), (max-width: 48rem) {
		.rb-dock :global(.btn) { min-height: var(--touch-target); }
		.rb-dock .rb-dock-ask { margin-bottom: var(--space-3); }
	}
	.rb-dock.receipt, .rb-dock.stale { flex-wrap: nowrap; column-gap: var(--space-2); padding-top: var(--space-2); }
	.rb-dock .rb-receipt { flex: 1 1 0; min-width: 0; color: var(--text-secondary); }
	.rb-dock .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.rb-dock.receipt .rb-icon { color: var(--green-text); }
	.rb-dock.receipt.changes .rb-icon, .rb-dock.stale .rb-icon { color: var(--amber-text); }
	.rb-dock.stale .rb-sub { margin-left: auto; opacity: 0.85; }

	/* One flat row inside the document header's chrome: no card, border or shadow of its own. The owner's answer controls (`.rb-actions`, `.rb-note`) are its snippet, so their rules reach them through `:global`. */
	.review-bar { --rb-hit: calc(var(--space-8) - var(--space-1)); flex: none; display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); padding: 0 var(--space-1) 0 0; font-size: var(--text-footnote); color: var(--text-secondary); min-width: 0; }
	/* The block grows from nothing (height through grid rows, clipped only while it moves) so the text below slides down instead of jumping; its own margin cancels the header's gap until it has height. An answer swaps in at the block's own height: the rows share one grid cell and crossfade (the transitions in the script). No transform on any ancestor of the capsule: it would make the floating capsule position against the row instead of the viewport. */
	.rb-slot { display: grid; grid-template-rows: 1fr; min-width: 0; animation: rb-grow var(--dur-standard) var(--ease-entrance) backwards, rb-clip var(--dur-standard) step-end; }
	/* `--rb-fade` rests at 1; the swap transition's tick overrides it inline on this same element while the ask's text crossfades. */
	.rb-slot > .review-bar { grid-area: 1 / 1; min-height: 0; --rb-fade: 1; }
	.rb-head, .rb-row2 { opacity: var(--rb-fade); }
	@keyframes rb-grow { from { grid-template-rows: 0fr; margin-top: calc(var(--space-1) * -1); opacity: 0; } }
	@keyframes rb-grow-solid { from { grid-template-rows: 0fr; margin-top: calc(var(--space-1) * -1); } }
	@keyframes rb-clip { from, to { overflow: hidden; } }
	@keyframes rb-capsule-in { from { transform: translateY(calc(100% + var(--space-3) + var(--safe-bottom))); } }
	.review-bar.receipt, .review-bar.stale { min-height: calc(var(--space-7) * 2); }
	/* The ask block is two lines at every width. Line 1: the asker, the ask on one line and the expand chevron (D8).
	   Line 2: a status slot on the left and the answer on the right. */
	/* The canvas already owns the document-review surface. Keep this row in its
	   existing header chrome; raised needs-you cards live only on chat objects. */
	.review-bar.ask {
		flex-direction: column; align-items: stretch; gap: 0;
		padding: 0 var(--space-1) 0 0;
		border: 0; border-radius: 0;
		background: transparent; box-shadow: none;
	}
	.rb-head { display: flex; flex-direction: column; min-width: 0; }
	.rb-line { display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); min-width: 0; padding: 0; border: 0; background: none; color: var(--text-primary); font: inherit; text-align: left; cursor: pointer; border-radius: var(--radius-sm); }
	.review-bar.static .rb-line { cursor: default; }
	.rb-line:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* The answer buttons draw the same ring as every other control around the sheets, at any width and on any engine, instead of the browser's own focus ring (which is orange in a mobile engine and blue-and-white in a desktop one), final the moment focus lands (`Button` transitions every property, which would grow the ring from the browser's to this one). */
	.review-bar :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; transition: none; }
	/* The avatar and name; it lays nothing out itself except in the narrow header below. */
	.rb-lead { display: contents; }
	/* The requester takes its natural width up to the header cap, leaving the ask the remaining one-line space. */
	.rb-who { flex: 0 1 auto; display: flex; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.rb-who strong { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: var(--fw-semibold); }
	/* This wrapper constrains the name; MiddleName owns its head/tail truncation within it. */
	.rb-plus { margin-left: var(--space-1); color: var(--text-secondary); font-weight: var(--fw-semibold); }
	/* A name cut in the middle keeps its first letter, the ellipsis, its last four characters and the colon (MiddleName's floor), whatever the row leaves it. */
	.rb-who:has(:global(.name.cut)) { min-width: calc(1.8em + 6ch); }
	/* The request keeps about 16 characters: a long name gives up its middle before the request is left with nothing. */
	.rb-ask { flex: 1 1 0; min-width: 16ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-secondary); }
	.email-review { margin-left: var(--space-px-2); }
	.email-review .rb-line { flex-wrap: wrap; }
	.email-review .rb-who, .email-review .rb-who strong { overflow: visible; white-space: normal; overflow-wrap: anywhere; }
	.email-review .rb-ask { flex: 1 1 12rem; min-width: 0; overflow: visible; white-space: normal; overflow-wrap: anywhere; }
	@media (max-width: 48rem) {
		.email-review { margin-left: calc(-1 * var(--doc-inset, 24px)); }
		.email-review .rb-line { flex-wrap: nowrap; }
		.email-review .rb-who, .email-review .rb-who strong { overflow: hidden; white-space: nowrap; }
		.email-review .rb-ask { flex: 1 1 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
	}
	.email-review:not(.noting) .rb-row2:has(.rb-status:empty) { display: none; }
	.rb-chev { color: var(--text-tertiary); transition: transform var(--dur-tight) var(--ease-snap); }
	.review-bar.expanded .rb-chev { transform: rotate(180deg); }
	/* Expanded, line 1 keeps its one row and the text moves below it, across the whole row (PE round 5 F-C). */
	.review-bar.expanded .rb-ask { display: none; }
	.review-bar.expanded .rb-chev { margin-left: auto; }
	.rb-ask-full { margin: 0; padding: 0 0 var(--space-1); color: var(--text-secondary); line-height: var(--lh-normal); overflow-wrap: anywhere; }
	.rb-others { margin: 0; padding: 0 0 var(--space-1) var(--space-6); list-style: none; color: var(--text-secondary); }
	.rb-others li + li { margin-top: var(--space-1); }
	.rb-others strong { color: var(--text-primary); font-weight: var(--fw-semibold); }
	.rb-row2 { display: flex; align-items: center; gap: var(--space-2); min-width: 0; min-height: var(--space-7); }
	.rb-status { flex: 1 1 0; min-width: 0; color: var(--amber-text); }
	.rb-status.busy { color: var(--text-secondary); }
	.review-bar.noting .rb-row2 { flex-wrap: wrap; }
	.review-bar.noting .rb-status { order: 3; flex-basis: 100%; min-height: 0; }
	.rb-capsule { display: contents; }
	/* While a comment sheet is up, one verdict group is on screen at a time: the answer row is not drawn, and gives up its height, so no blank band is left where it was and a landscape sheet does not cover the text under the bar. The phone capsule below slides away instead. */
	.review-bar.ask.suppressed .rb-row2 { display: none; }
	/* In the header, what Approve also accepts is the status slot's neutral line beside the buttons, as it was: it takes the room the buttons leave, and the empty status gives it up. */
	.rb-capsule :global(.rb-derived) { flex: 1 1 0; min-width: 0; margin: 0; color: var(--text-secondary); }
	.rb-capsule:has(:global(.rb-derived)) .rb-status:empty { flex: 0 0 0; }
	.rb-capsule :global(.rb-actions) { display: inline-flex; align-items: center; gap: var(--space-1); flex: 0 1 auto; flex-wrap: wrap; justify-content: flex-end; min-width: 0; margin-left: auto; }
	/* The note grows at every width, with the same bounded scrolling used by the narrow canvas. */
	.rb-capsule :global(.rb-note), .rb-dock :global(.rb-note) { flex: 1 1 10rem; min-width: 0; box-sizing: border-box; height: var(--space-6); min-height: var(--space-6); max-height: calc(4lh + 2 * var(--space-1)); padding: 0 var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--text-primary) 5%, transparent); color: var(--text-primary); font-family: var(--font-sans); font-size: var(--text-input); line-height: var(--space-6); resize: none; overflow-y: auto; }
	.rb-capsule :global(.rb-note::placeholder), .rb-dock :global(.rb-note::placeholder) { color: var(--text-tertiary); }
	.rb-capsule :global(.rb-note:focus), .rb-dock :global(.rb-note:focus) { outline: 2px solid var(--accent); outline-offset: -2px; border-color: transparent; }
	.rb-icon { width: var(--text-subhead); height: var(--text-subhead); flex: none; fill: none; stroke: currentColor; stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }
	.review-bar.receipt .rb-icon { color: var(--green-text); }
	.review-bar.receipt.changes .rb-icon { color: var(--amber-text); }
	.review-bar.receipt .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.rb-receipt { flex: 1 1 0; min-width: 0; outline: none; }
	.rb-seg { white-space: nowrap; }
	/* The rest of the receipt: the clip box is `.rb-rest` (a block with no padding on its inline start, so its edge is its content edge); the row inside starts one separator width to its left. */
	.rb-rest { display: block; overflow-x: clip; --rb-sep: 0.9em; }
	.rb-rest-row { display: block; margin-inline-start: calc(var(--rb-sep) * -1); }
	.rb-dot { display: inline-block; width: var(--rb-sep); text-align: center; }
	.rb-first { margin-inline-start: var(--rb-sep); }
	.review-bar.stale .rb-icon { color: var(--amber-text); }
	.review-bar.stale .rb-text { color: var(--text-primary); font-weight: var(--fw-medium); }
	.review-bar.stale .rb-sub { margin-left: auto; opacity: 0.85; }

	/* Expanded, the ask text (`.rb-ask-full`) is its own grid item spanning every column under line 1, so it has the header's full width, not what the pill and Request review leave it. */
	/* A narrow column, at any pointer, gives the note its own full-width row (up to four lines) above Cancel and the note's send button, as the phone capsule does. */
	/* There, a status takes its own row above the buttons too, rather than wrapping a word per line beside them. */
	/* 37.5rem is the document canvas's 600px narrow breakpoint at the default root size. */
	@container doc-canvas (max-width: 37.5rem) {
		.review-bar.ask .rb-row2 { flex-wrap: wrap; }
		.review-bar.ask .rb-status:not(:empty) { flex-basis: 100%; }
		.review-bar.noting :global(.rb-note) { flex: 1 0 100%; order: 1; height: auto; min-height: var(--space-6); max-height: calc(4lh + 2 * var(--space-1)); padding: var(--space-1) var(--space-2); line-height: var(--lh-normal); }
		.review-bar.noting :global(.rb-actions) { flex: 1 0 100%; order: 2; justify-content: flex-end; margin-left: 0; }
	}

	/* What Approve also accepts is short and belongs to that button, so it keeps the buttons' row until the canvas is too narrow for both. A refusal or a sending message still takes its own row. 32rem is 512px here (a container query's rem is 16px whatever the root size), a round width above the measured need at the app's 15px root: the row's insets (44px + 24px), Send back + Approve (153px), the two 8px gaps and the longest line, 'Also accepts 99 suggestions' (157px; 150px at 2) come to 395px (387px at 2), so the line keeps the buttons' row from 395px up. */
	@container doc-canvas (max-width: 32rem) {
		.review-bar.ask .rb-capsule :global(.rb-derived) { flex-basis: 100%; }
	}

	/* A touch screen at a narrow canvas gets 44px targets, and the ask is one in-flow line. */
	@media (any-pointer: coarse) {
		@container doc-canvas (max-width: 37.5rem) {
			.review-bar { min-height: var(--rb-hit); }
			.review-bar.ask { min-height: 0; }
			.rb-line { min-height: var(--rb-hit); }
			.review-bar.receipt, .review-bar.stale { min-height: var(--rb-hit); }
			.rb-capsule :global(.btn) { min-height: var(--rb-hit); }
		}
	}

	/* Solid, not glass: document text reads through any translucent fill, and the header's backdrop root stops the blur from reaching it. A phone-width touch screen at a narrow canvas gets the answer in a floating capsule at the canvas's bottom edge, with the status above its buttons (the capsule grows upward). The capsule is fixed to the window, which is the canvas only in the phone layout: a wider touch screen beside the chat keeps the answer in the bar's row. 48rem is the viewport store's 768px phone breakpoint at the default root size. */
	@media (any-pointer: coarse) and (max-width: 48rem) {
		@container doc-canvas (max-width: 37.5rem) {
			.rb-row2, .review-bar.ask.suppressed .rb-row2 { display: contents; }
			.rb-slot { animation-name: rb-grow-solid, rb-clip; }
			/* The capsule slides from below the safe area and back, fully opaque the whole way (RSR-3): no ancestor fades, and its own motion is transform only. */
			.rb-capsule { position: fixed; flex-wrap: wrap; z-index: 5; left: var(--space-3); right: var(--space-3); bottom: calc(var(--space-3) + var(--safe-bottom)); display: flex; align-items: center; gap: var(--space-2); padding: var(--space-2); border: 1px solid var(--border-subtle); border-radius: var(--radius-xl); background: var(--bg-secondary); box-shadow: var(--glass-elevation-1); animation: rb-capsule-in var(--dur-standard) var(--ease-entrance) backwards; transition: transform var(--dur-standard) var(--ease-entrance), visibility 0s; }
			.rb-capsule.suppressed { transform: translateY(calc(100% + var(--space-3) + var(--safe-bottom))); pointer-events: none; visibility: hidden; transition-duration: calc(var(--dur-standard) * 0.7), 0s; transition-delay: 0s, calc(var(--dur-standard) * 0.7); }
			.review-bar.ask .rb-capsule .rb-status { order: -1; flex: 1 0 100%; padding: 0 var(--space-2); }
			.review-bar.ask .rb-capsule .rb-status:empty { display: none; }
			.rb-capsule :global(.rb-actions) { flex: 1 1 auto; flex-wrap: wrap; margin-left: 0; }
			.rb-capsule :global(.btn) { flex: 1 1 auto; min-width: 0; }
			/* Writing a note: the capsule grows upward into two rows, the field across the full width (up to four lines), then Cancel and the send button right-aligned (PE B3). */
			.rb-capsule :global(.rb-note) { flex: 1 0 100%; min-height: var(--rb-hit); max-height: calc(4lh + 2 * var(--space-2)); padding: var(--space-2); font-size: var(--text-input); line-height: var(--lh-normal); }
			.review-bar.noting .rb-capsule :global(.rb-actions) { flex: 1 0 100%; justify-content: flex-end; }
			.review-bar.noting .rb-capsule :global(.rb-actions .btn) { flex: 0 0 auto; min-width: max-content; }
			.review-bar.email-review .rb-row2 { display: flex; }
			.review-bar.email-review:not(.noting) .rb-row2:has(.rb-status:empty) { display: none; }
			.review-bar.email-review .rb-capsule { position: static; display: contents; animation: none; }
			.review-bar.email-review .rb-capsule :global(.btn) { min-height: var(--touch-target); }
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.rb-slot { animation: rb-fade var(--dur-tight) var(--ease-snap) backwards; }
		@keyframes rb-fade { from { opacity: 0; } }
	}
</style>
