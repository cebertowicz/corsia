<script lang="ts">
	import { onDestroy, tick } from 'svelte';
	import { fade, slide } from 'svelte/transition';
	import { portal } from '$lib/actions/portal';
	import { ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { shellInfo, startLocalService } from '$lib/remoteShell';
	import { restartToFinishUpdate } from '$lib/update/restartToFinish';
	import { dismissUpdateScreen, updateScreenView } from '$lib/update/screenStore';
	import { retryUpdate, updateStarting } from '$lib/update/store';
	import Button from './ui/Button.svelte';
	import LivingMark from './LivingMark.svelte';

	const view = $derived($updateScreenView);
	const shell = $derived(shellInfo());
	const localDesktop = $derived(shell?.mode === 'local');
	const hostName = $derived(shell?.mode === 'remote' ? shell.name : 'the computer CORSIA runs on');

	let starting = $state(false);
	let startFailed = $state(false);
	let manualStartFailed = $state(false);
	let updateFinishing = $state(false);
	let autoStartAttempted = $state(false);
	let startActionEl: HTMLButtonElement | undefined = $state();
	let screenEl: HTMLElement | undefined = $state();

	const longer = $derived(view?.mode === 'longer');
	const recovery = $derived(longer || view?.mode === 'start_failed' || (view?.mode === 'failed' && view.serviceDown));
	const autoStartWaiting = $derived(longer && localDesktop && autoStartAttempted && !starting && !startFailed && !updateFinishing);

	$effect(() => {
		if (view === null) {
			starting = false;
			startFailed = false;
			manualStartFailed = false;
			updateFinishing = false;
			autoStartAttempted = false;
			return;
		}
		if ((view.mode === 'start_failed' || (view.mode === 'failed' && view.serviceDown)) && !starting) startFailed = true;
		if (longer && localDesktop && !autoStartAttempted) {
			autoStartAttempted = true;
			void startCorsia(true);
		}
	});
	$effect(() => {
		if (recovery && localDesktop && startFailed && !updateFinishing) void tick().then(() => startActionEl?.focus());
	});
	$effect(() => {
		if (screenEl) screenEl.focus({ preventScroll: true });
	});

	async function startCorsia(automatic = false): Promise<void> {
		starting = true;
		startFailed = false;
		manualStartFailed = false;
		updateFinishing = false;
		try {
			const outcome = await startLocalService();
			if (outcome === 'update_finishing') updateFinishing = true;
		} catch {
			startFailed = true;
			manualStartFailed = !automatic;
			if (!automatic) autoStartAttempted = true;
		} finally {
			starting = false;
		}
	}

	function retry(): void {
		void (view?.failure?.retryAction === 'restart' ? restartToFinishUpdate() : retryUpdate());
	}

	function dismiss(): void {
		dismissUpdateScreen();
	}

	function actionsReveal(node: Element, { duration }: { duration: number }) {
		const fading = fade(node, { duration });
		if ($prefersReducedMotion) return fading;
		const sliding = slide(node, { duration });
		return { duration, easing: sliding.easing, css: (t: number, u: number) => `${sliding.css?.(t, u) ?? ''};${fading.css?.(t, u) ?? ''}` };
	}

	onDestroy(() => { starting = false; });
</script>

{#if view}
	<div
		class="update-screen"
		use:portal
		bind:this={screenEl}
		tabindex="-1"
		role="dialog"
		aria-modal="true"
		aria-labelledby="update-screen-title"
		in:fade={{ duration: ms('--dur-tight') }}
	>
		<div class="column">
			<div class="mark"><LivingMark entry="none" /></div>
			<h2 id="update-screen-title">{view.mode === 'done' ? `Updated to ${view.toVersion ?? ''}`.trim() : view.mode === 'failed' && view.failure ? view.failure.title : 'Updating CORSIA'}</h2>
			<div class="step" role="status" aria-live="polite" aria-atomic="true">
				{#if view.mode === 'failed' && !view.serviceDown && view.failure}
					<p class="failure-headline">{view.failure.headline}</p>
					<p class="detail">{view.failure.body}</p>
				{:else if view.mode !== 'done'}
					<p class="step-label">{view.label}</p>
				{/if}
			</div>
			<div class="percent" data-state={view.mode} aria-hidden="true">{view.estimated ? 'About ' : ''}{view.mode === 'done' ? '100' : view.percent.toFixed(1)}%</div>
			<div class="bar" role="progressbar" aria-label={view.mode === 'failed' && view.failure ? view.failure.title : view.label} aria-valuemin="0" aria-valuemax="100" aria-valuenow={view.percent}>
				<i style:width={`${view.percent}%`}></i>
			</div>
			{#if view.fromVersion && view.toVersion && view.mode !== 'done'}
				<p class="versions">{view.fromVersion} → {view.toVersion}</p>
			{/if}
			<div class="copy">
				{#if view.mode === 'failed' && !view.serviceDown}
					<div class="actions" in:actionsReveal={{ duration: ms('--dur-tight') }}>
						{#if view.failure?.retryLabel}<Button variant="primary" loading={$updateStarting} onclick={retry}>{view.failure.retryLabel}</Button>{/if}
						<Button variant="ghost" onclick={dismiss}>Dismiss</Button>
					</div>
				{:else if view.mode === 'done'}
					<p class="detail">CORSIA comes back by itself. You can keep this window open.</p>
				{:else if recovery}
					{#if longer}<p class="detail strong">This is taking longer than usual.</p>{/if}
					{#if localDesktop}
						{#if updateFinishing}
							<p class="detail">An update is finishing. This can take a few minutes. It comes back by itself.</p>
						{:else if autoStartWaiting}
							<p class="detail">It’s taking longer than usual. CORSIA is still starting. Nothing you need to do yet.</p>
						{:else if manualStartFailed}
							<p class="detail" role="alert">It still didn’t start. Click Start CORSIA to try again, or quit CORSIA with ⌘Q and open it again.</p>
						{:else if startFailed}
							<p class="detail">It didn’t start on its own.</p>
						{/if}
						{#if startFailed && !updateFinishing}
							<div class="actions" in:actionsReveal={{ duration: ms('--dur-tight') }}>
								<Button variant="primary" onclick={() => void startCorsia()} loading={starting} bind:el={startActionEl}>Start CORSIA</Button>
							</div>
						{/if}
					{:else}
						<p class="detail">Open CORSIA on {hostName}.</p>
						<p class="detail">Progress shows on the host computer, {hostName}.</p>
					{/if}
				{:else}
					<p class="detail">CORSIA comes back by itself. You can keep this window open.</p>
					{#if view.estimated}<p class="detail">Progress shows on the host computer, {hostName}.</p>{/if}
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.update-screen { position: fixed; inset: 0; z-index: 10000; display: grid; place-items: center; padding: var(--space-5); box-sizing: border-box; overflow-y: auto; background: var(--bg-primary); color: var(--text-primary); outline: none; }
	.column { display: flex; flex-direction: column; align-items: center; min-width: 0; width: min(100%, 420px); text-align: center; }
	.mark { width: calc(var(--space-8) + var(--space-6)); height: calc(var(--space-8) + var(--space-6)); margin-bottom: var(--space-4); }
	h2 { margin: 0; font-size: var(--text-title2); font-weight: var(--fw-semibold); color: var(--text-primary); }
	.step { min-height: calc(var(--text-body) * 1.5); margin-top: var(--space-2); }
	.step p, .detail { margin: 0; }
	.step-label, .detail { color: var(--text-secondary); font-size: var(--text-body); }
	.failure-headline { margin: 0; color: var(--text-primary); font-size: var(--text-body); font-weight: var(--fw-semibold); }
	.percent { margin-top: var(--space-4); font-size: calc(var(--text-title1) * 2); line-height: 1.1; font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; color: var(--text-primary); }
	.percent[data-state='failed'] { color: var(--text-tertiary); }
	.bar { width: 100%; height: var(--space-2); margin-top: var(--space-3); border-radius: var(--radius-pill); overflow: hidden; background: var(--bg-tertiary); }
	.bar i { display: block; height: 100%; background: var(--accent); border-radius: inherit; transition: width var(--dur-hero-plus) linear; }
	.versions { margin: var(--space-3) 0 0; color: var(--text-tertiary); font-size: var(--text-footnote); font-variant-numeric: tabular-nums; }
	.copy { margin-top: var(--space-4); display: flex; flex-direction: column; align-items: center; gap: var(--space-1); min-height: calc(var(--text-body) * 3); }
	.detail.strong { color: var(--text-primary); }
	.actions { display: flex; flex-wrap: wrap; justify-content: center; gap: var(--space-2); margin-top: var(--space-3); }
	h2, .step, .copy, .versions, .detail, .failure-headline { min-width: 0; max-width: 100%; overflow-wrap: anywhere; }
	@media (prefers-reduced-motion: reduce) { .bar i { transition: none; } }
</style>
