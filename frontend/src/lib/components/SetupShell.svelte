<script lang="ts">
	import type { Snippet } from 'svelte';
	import { onDestroy, tick, untrack } from 'svelte';
	import { fade } from 'svelte/transition';
	import { Button, RailRow, RailSection } from '$lib/components/ui';
	import { peerFade } from '$lib/popoverMotion';

	export type SetupStep = { label: string; icon: 'sparkle' | 'person' | 'chip'; state: 'current' | 'todo' | 'done' };
	export type SetupJob = { label: string; state: 'busy' | 'done' | 'error'; meta?: string; progress?: number };

	interface Props {
		steps: SetupStep[];
		jobs?: SetupJob[];
		page: string;
		title: string;
		lede?: string;
		primary?: { label: string; disabled?: boolean; loading?: boolean; note?: string };
		mark?: boolean;
		onPrimary?: () => void;
		secondary?: Snippet;
		children?: Snippet;
	}

	const { steps, jobs = [], page, title, lede, primary, mark = false, onPrimary, secondary, children }: Props = $props();
	let shownJobs = $state<SetupJob[]>(untrack(() => jobs));
	let jobRegionOpen = $state(untrack(() => jobs.length > 0));
	let sceneHeading = $state<HTMLHeadingElement | null>(null);
	let primaryButton = $state<HTMLButtonElement | null>(null);
	let primaryLabelDuration = $state(0);
	let previousPrimaryLabel: string | undefined;
	let flipVersion = 0;
	let pendingWidthFrame: number | undefined;
	let removeWidthTransitionListener: (() => void) | undefined;
	let mounted = false;

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

	function readPrimaryLabelDuration(node: HTMLElement | null) {
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
</script>

<div class="setup-shell">
	<aside class="rail" aria-label="Setup progress">
		<div class="traffic-space" data-tauri-drag-region></div>
		<RailSection label="Setup" first>
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
			{#key page}
				<section class="pane-scene" data-setup-page={page} in:peerFade out:peerFade onoutrostart={(event) => (event.currentTarget as HTMLElement).inert = true}>
					<header>{#if mark}<img class="mark" src="/icons/corsia-mark.svg" alt="" />{/if}<h1 bind:this={sceneHeading} tabindex="-1">{title}</h1></header>
					{#if lede}<p class="lede">{lede}</p>{/if}
					<div class="body">{@render children?.()}</div>
				</section>
			{/key}
		</div>
		<footer>
			<div>{@render secondary?.()}</div>
			{#if primary}
				<div class="primary-action">
					{#if primary.disabled && primary.note}<span class="footer-note">{primary.note}</span>{/if}
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
	.pane { position: relative; flex: 1; min-width: 0; min-height: 100dvh; display: flex; flex-direction: column; padding: calc(var(--space-8) + var(--space-4)) var(--space-4) var(--space-6); }
	.drag { position: absolute; inset: 0 0 auto; height: var(--space-8); }
	.pane-scenes { display: grid; flex: 1; min-height: 0; }
	.pane-scene { grid-area: 1 / 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
	header { display: flex; align-items: center; gap: var(--space-2); min-height: var(--space-7); }
	.mark { width: var(--space-5); height: var(--space-5); }
	h1 { margin: 0; font-size: var(--text-title2); font-weight: var(--fw-semibold); letter-spacing: var(--ls-title); }
	.pane-scene h1:focus { outline: none; }
	.lede { max-width: calc(var(--space-8) * 9); margin: var(--space-3) 0 0; color: var(--text-secondary); line-height: var(--lh-normal); }
	.body { flex: 1; min-height: 0; margin-top: var(--space-5); padding-bottom: var(--space-6); }
	footer { display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); min-height: var(--space-7); }
	.primary-action { display: flex; align-items: center; gap: var(--space-3); }
	.primary-label-stack { display: grid; grid-template-areas: 'label'; }
	.primary-label { grid-area: label; }
	:global(.primary-label-exiting) { position: absolute; inset: 0; display: grid; place-items: center; }
	/* The pill clips the exiting label while it shrinks; scoped here so the shared pill keeps its touch-target ::after. */
	:global(.primary-action .btn.pill) { overflow: hidden; }
	:global(.primary-action .btn.pill) { interpolate-size: allow-keywords; transition: width var(--dur-tight) var(--ease-snap), min-width var(--dur-tight) var(--ease-snap), background var(--dur-tight) var(--ease-snap), border-color var(--dur-tight) var(--ease-snap), color var(--dur-tight) var(--ease-snap), opacity var(--dur-tight) var(--ease-snap), transform var(--dur-tight) var(--ease-snap); }
	.footer-note { max-width: calc(var(--space-8) * 6); color: var(--text-tertiary); font-size: var(--text-footnote); text-align: right; }
	.job-region { overflow: hidden; }
	.job-region-inner { min-height: 0; overflow: hidden; }
	.job-row { min-height: 0; display: grid; grid-template-rows: 1fr; overflow: hidden; }
	.job-row-inner { min-height: 0; overflow: hidden; }
</style>
