<script lang="ts">
	import type { Snippet } from 'svelte';
	import { onDestroy, tick, untrack } from 'svelte';
	import { fade } from 'svelte/transition';
	import { Button, RailRow, RailSection } from '$lib/components/ui';
	import { peerFade } from '$lib/popoverMotion';
	import { isSetupWide } from '$lib/stores/viewport';
	import LivingMark from './LivingMark.svelte';

	export type SetupStep = { label: string; icon: 'sparkle' | 'person' | 'chip' | 'computer' | 'code' | 'card'; state: 'current' | 'todo' | 'done' };
	export type SetupJob = { label: string; state: 'busy' | 'done' | 'error'; meta?: string; progress?: number; action?: { label: string; onclick: () => void } };

	interface Props {
		steps: SetupStep[];
		/** The sidebar section's grey caps: Setup, or Connect on the connect-to-another-computer screens. */
		section?: string;
		jobs?: SetupJob[];
		page: string;
		title: string;
		lede?: string;
		primary?: { label: string; disabled?: boolean; loading?: boolean; note?: string; noteAlways?: boolean };
		onPrimary?: () => void;
		secondary?: Snippet;
		children?: Snippet;
	}

	const { steps, section = 'Setup', jobs = [], page, title, lede, primary, onPrimary, secondary, children }: Props = $props();
	let shownJobs = $state<SetupJob[]>(untrack(() => jobs));
	let jobRegionOpen = $state(untrack(() => jobs.length > 0));
	let sceneHeading = $state<HTMLHeadingElement | null>(null);
	let primaryButton = $state<HTMLButtonElement | undefined>();
	let primaryLabelDuration = $state(0);
	let previousPrimaryLabel: string | undefined;
	let flipVersion = 0;
	let pendingWidthFrame: number | undefined;
	let removeWidthTransitionListener: (() => void) | undefined;
	let mounted = false;
	let mark = $state<LivingMark | undefined>();

	/** Plays the mark's exit and resolves when it is done (at once where no mark shows or motion is reduced). Setup calls this once, as it hands over to the first chat. */
	export function exitMark(): Promise<void> {
		return mark ? mark.exit() : Promise.resolve();
	}

	$effect(() => {
		const scene = page;
		if (!mounted) {
			mounted = true;
			return;
		}
		void tick().then(() => {
			if (page === scene) sceneHeading?.focus();
		});
	});

	$effect(() => {
		if (jobs.length > 0) {
			shownJobs = jobs;
			jobRegionOpen = true;
		} else {
			jobRegionOpen = false;
		}
	});

	function clearClosingJobs() {
		if (jobs.length === 0) shownJobs = [];
	}

	function readPrimaryLabelDuration(node: HTMLElement | undefined) {
		return node ? Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 0 : 0;
	}

	function clearWidthTransitionListener() {
		if (pendingWidthFrame !== undefined) cancelAnimationFrame(pendingWidthFrame);
		pendingWidthFrame = undefined;
		removeWidthTransitionListener?.();
		removeWidthTransitionListener = undefined;
	}

	function makeOutgoingLabelAbsolute(event: Event) {
		(event.currentTarget as HTMLElement).classList.add('primary-label-exiting');
	}

	$effect.pre(() => {
		const nextLabel = primary?.label;
		if (nextLabel === previousPrimaryLabel) return;

		const previousLabel = previousPrimaryLabel;
		previousPrimaryLabel = nextLabel;
		primaryLabelDuration = readPrimaryLabelDuration(primaryButton);
		if (!primaryButton || !previousLabel || !nextLabel) {
			if (!nextLabel) {
				flipVersion += 1;
				clearWidthTransitionListener();
				primaryButton?.style.removeProperty('width');
			}
			return;
		}

		const button = primaryButton;
		const oldWidth = button.getBoundingClientRect().width;
		const transitionVersion = ++flipVersion;
		clearWidthTransitionListener();
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
			button.style.removeProperty('width');
			return;
		}

		void tick().then(() => {
			if (transitionVersion !== flipVersion || primaryButton !== button) return;

			const inlineTransition = button.style.transition;
			button.style.transition = 'none';
			button.style.width = 'auto';
			const newWidth = button.getBoundingClientRect().width;
			button.style.width = `${oldWidth}px`;
			void button.offsetWidth;
			button.style.transition = inlineTransition;
			if (Math.abs(newWidth - oldWidth) < 0.01) {
				button.style.removeProperty('width');
				return;
			}

			const onTransitionEnd = (event: TransitionEvent) => {
				if (event.target !== button || event.propertyName !== 'width') return;
				button.style.removeProperty('width');
				clearWidthTransitionListener();
			};
			button.addEventListener('transitionend', onTransitionEnd);
			removeWidthTransitionListener = () => button.removeEventListener('transitionend', onTransitionEnd);
			pendingWidthFrame = requestAnimationFrame(() => {
				pendingWidthFrame = undefined;
				if (transitionVersion !== flipVersion || primaryButton !== button) return;
				button.style.width = `${newWidth}px`;
			});
		});
	});

	onDestroy(() => {
		flipVersion += 1;
		clearWidthTransitionListener();
		primaryButton?.style.removeProperty('width');
	});

	function jobHeightMotion(node: Element, _params: undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
		const style = getComputedStyle(node);
		const tight = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 0;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return fade(node, { duration: tight });
		const standard = Number.parseFloat(style.getPropertyValue('--dur-standard')) || 0;
		return {
			duration: direction === 'out' ? standard * 0.7 : standard,
			css: (t: number) => `grid-template-rows: ${t}fr; opacity: ${t}`,
		};
	}

	function jobRegionMotion(node: Element, _params: undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
		const style = getComputedStyle(node);
		const tight = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 0;
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return fade(node, { duration: tight });
		const standard = Number.parseFloat(style.getPropertyValue('--dur-standard')) || 0;
		const height = node.getBoundingClientRect().height;
		return {
			duration: direction === 'out' ? standard * 0.7 : standard,
			css: (t: number) => `height: ${t * height}px; opacity: ${t}; overflow: hidden`,
		};
	}

	/**
	 * Marks the body `data-more` while content sits under the dock edge and `data-above` while it sits under the title
	 * and intro, so each edge fades its content out only on the side where there is more to scroll to.
	 */
	function scrollCue(node: HTMLElement) {
		const sync = () => {
			node.dataset.more = String(node.scrollTop + node.clientHeight < node.scrollHeight - 1);
			node.dataset.above = String(node.scrollTop > 1);
		};
		node.addEventListener('scroll', sync, { passive: true });
		const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(sync);
		resize?.observe(node);
		const mutate = typeof MutationObserver === 'undefined' ? null : new MutationObserver(sync);
		mutate?.observe(node, { childList: true, subtree: true });
		sync();
		return { destroy() { node.removeEventListener('scroll', sync); resize?.disconnect(); mutate?.disconnect(); } };
	}

	function sceneMotion(node: HTMLElement, params: undefined, options: { direction: 'in' | 'out' | 'both' }) {
		if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
			const tight = Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 0;
			return fade(node, { duration: tight });
		}
		return peerFade(node, params, options);
	}
</script>

<div class="setup-shell">
	<aside class="rail" aria-label="Setup progress">
		<div class="traffic-space" data-tauri-drag-region></div>
		<RailSection label={section} first>
			{#each steps as step (step.label)}
				<RailRow label={step.label} iconName={step.icon} state={step.state} />
			{/each}
		</RailSection>
		{#if jobRegionOpen}
			<div class="job-region" in:jobRegionMotion out:jobRegionMotion onoutroend={clearClosingJobs}>
				<div class="job-region-inner">
					<RailSection label="In the background">
						{#each shownJobs as job (job.label)}
							<div class="job-row" in:jobHeightMotion out:jobHeightMotion><div class="job-row-inner"><RailRow {...job} /></div></div>
						{/each}
					</RailSection>
				</div>
			</div>
		{/if}
	</aside>
	<main class="pane">
		<div class="drag" data-tauri-drag-region></div>
		<div class="pane-scenes">
			<!-- Outside the keyed scene, so a screen change never rebuilds the mark or restarts its motion. From 1280 wide it fills the right half. -->
			{#if $isSetupWide}<div class="brand"><LivingMark bind:this={mark} /></div>{/if}
			{#key page}
				<section class="pane-scene" data-setup-page={page} in:sceneMotion out:sceneMotion onoutrostart={(event) => (event.currentTarget as HTMLElement).inert = true}>
					<header><h1 bind:this={sceneHeading} tabindex="-1">{title}</h1></header>
					{#if lede}<p class="lede">{lede}</p>{/if}
					<div class="body" use:scrollCue>{@render children?.()}</div>
				</section>
			{/key}
		</div>
		<footer>
			<div>{@render secondary?.()}</div>
			{#if primary}
				<div class="primary-action">
					{#if primary.note && (primary.disabled || primary.noteAlways)}<span class="footer-note">{primary.note}</span>{/if}
					<Button variant="pill" disabled={primary.disabled} loading={primary.loading} onclick={onPrimary} bind:el={primaryButton}>
						<span class="primary-label-stack">
							{#key primary.label}
								<span class="primary-label" in:fade={{ duration: primaryLabelDuration }} out:fade={{ duration: primaryLabelDuration }} onoutrostart={makeOutgoingLabelAbsolute}>{primary.label}</span>
							{/key}
						</span>
					</Button>
				</div>
			{/if}
		</footer>
	</main>
</div>

<style>
	.setup-shell { min-height: 100dvh; display: flex; background: var(--bg-primary); color: var(--text-primary); }
	.rail { width: var(--sidebar-width); flex: none; border-right: 1px solid var(--border-subtle); background: var(--glass-thick); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); }
	.traffic-space { height: calc(var(--space-7) + var(--space-5)); }
	.pane { position: relative; flex: 1; min-width: 0; height: 100dvh; box-sizing: border-box; display: flex; flex-direction: column; padding: calc(var(--space-8) + var(--space-4)) var(--space-4) var(--space-6) var(--space-7); }
	.drag { position: absolute; inset: 0 0 auto; height: var(--space-8); }
	.pane-scenes { display: grid; flex: 1; min-height: 0; }
	.pane-scene { position: relative; grid-area: 1 / 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	header { display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); }
	/* From 1280 wide every scene is two columns: content on the left, the mark in the right half. Below that there is no mark. The mark is placed against the pane, not the scene column, so a taller footer (a note beside Continue) never moves it between steps. */
	.brand { position: absolute; top: calc(var(--space-8) + var(--space-4) + var(--space-7) + var(--space-3)); right: 0; bottom: calc(var(--space-6) + var(--space-7) + var(--space-2)); width: 50%; box-sizing: border-box; padding-left: var(--space-5); pointer-events: none; }
	@media (min-width: 80em) {
		.pane-scene header, .pane-scene .lede, .pane-scene .body { width: 50%; box-sizing: border-box; padding-right: var(--space-5); }
	}
	h1 { margin: 0; font-size: var(--text-title1); font-weight: var(--fw-semibold); letter-spacing: var(--ls-title); }
	.pane-scene h1:focus { outline: none; }
	.lede { max-width: none; margin: var(--space-3) 0 0; color: var(--text-secondary); font-size: var(--text-title3); line-height: var(--lh-normal); }
	.body { flex: 1; min-height: 0; margin-top: var(--space-5); padding-bottom: var(--space-6); /* a scene taller than the window scrolls here, so the dock stays in view */ overflow-y: auto; overflow-x: hidden; /* the connect code screen bleeds its focus-ring room outward, which must not scroll sideways */ scrollbar-width: thin; }
	/* The attributes are set by the scrollCue action, which Svelte cannot see: without :global it prunes the rule as unused. */
	.body:global([data-more='true']), .body:global([data-above='true']) { --cue-top: 0px; --cue-bottom: 0px; mask-image: linear-gradient(to bottom, transparent, black var(--cue-top), black calc(100% - var(--cue-bottom)), transparent); }
	.body:global([data-above='true']) { --cue-top: var(--space-4); }
	.body:global([data-more='true']) { --cue-bottom: var(--space-4); }
	/* Bottom-aligned and as tall as the pill (--space-7 + --space-2) whether or not a primary or a note renders, so Back and Continue sit in the same place on every step. */
	footer { display: flex; align-items: flex-end; justify-content: space-between; gap: var(--space-4); min-height: calc(var(--space-7) + var(--space-2)); }
	footer > div { display: flex; align-items: center; gap: var(--space-1); min-height: calc(var(--space-7) + var(--space-2)); }
	.primary-action { display: flex; align-items: center; gap: var(--space-3); }
	.primary-label-stack { display: grid; grid-template-areas: 'label'; }
	.primary-label { grid-area: label; }
	:global(.primary-label-exiting) { position: absolute; inset: 0; display: grid; place-items: center; }
	/* The pill clips the exiting label while it shrinks; scoped here so the shared pill keeps its touch-target ::after. */
	:global(.primary-action .btn.pill) { overflow: hidden; }
	:global(.primary-action .btn.pill) { interpolate-size: allow-keywords; transition: width var(--dur-tight) var(--ease-snap), min-width var(--dur-tight) var(--ease-snap), background var(--dur-tight) var(--ease-snap), border-color var(--dur-tight) var(--ease-snap), color var(--dur-tight) var(--ease-snap), opacity var(--dur-tight) var(--ease-snap), transform var(--dur-tight) var(--ease-snap); }
	.footer-note { max-width: calc(var(--space-8) * 6); color: var(--text-tertiary); font-size: var(--text-subhead); text-align: right; }
	/* The setup floor (800 x 600): tighten the spacing before anything scrolls. */
	@media (max-height: 43.75em) {
		.pane { padding-top: var(--space-8); }
		.lede { margin-top: var(--space-2); }
		.brand { top: calc(var(--space-8) + var(--space-7) + var(--space-2)); }
		.body { margin-top: var(--space-3); padding-bottom: var(--space-2); }
	}
	@media (max-width: 40em) {
		.rail { display: none; }
		.primary-action { flex-direction: column; align-items: flex-end; }
	}
	.job-region { overflow: hidden; }
	.job-region-inner { min-height: 0; overflow: hidden; }
	.job-row { min-height: 0; display: grid; grid-template-rows: 1fr; overflow: hidden; }
	.job-row-inner { min-height: 0; overflow: hidden; }
</style>
