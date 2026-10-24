<script lang="ts">
	import { onDestroy, tick, untrack } from 'svelte';
	import { fade, slide } from 'svelte/transition';
	import { portal } from '$lib/actions/portal';
	import { ms } from '$lib/popoverMotion';
	import { prefersReducedMotion } from '$lib/stores/viewport';
	import { onStartupShellPage, recoverLocalService, shellInfo, type RecoveryIntent } from '$lib/remoteShell';
	import { restartToFinishUpdate } from '$lib/update/restartToFinish';
	import { dismissUpdateScreen, handOverScreen, updateScreenView } from '$lib/update/screenStore';
	import { currentTheme, encodeHandoff, keepScreenInAddress, keptReturnRoute, moveWindow, returnUrl, SHELL_UPDATE_PAGE } from '$lib/update/shellHandoff';
	import { launchReadyUrl, startupProgress } from '$lib/update/startupProgress';
	import { retryUpdate, updateStarting } from '$lib/update/store';
	import Button from './ui/Button.svelte';
	import LivingMark from './LivingMark.svelte';

	const view = $derived($updateScreenView);
	const shell = $derived(shellInfo());
	const localDesktop = $derived(shell?.mode === 'local');
	const hostName = $derived(shell?.mode === 'remote' && shell.name.trim() ? shell.name : null);
	// Only the shell's own page may ask for a start; a local-service page hands the screen to it instead.
	const onShell = onStartupShellPage();

	let starting = $state(false);
	let startFailed = $state(false);
	let manualStartFailed = $state(false);
	let autoStartAttempted = $state(false);
	/** An update or start holds the lock: nothing was done, the screen keeps waiting and asks again on its next second. */
	let deferred = $state(false);
	/** The start that was deferred, asked again as itself (a press stays a press) until an answer that isn't deferred. */
	let pending: RecoveryIntent | null = null;
	let askedAt = 0;
	let handedOver = false;
	let returned = false;
	let startActionEl: HTMLButtonElement | undefined = $state();
	let screenEl: HTMLElement | undefined = $state();

	const longer = $derived(view?.mode === 'longer');
	const recovery = $derived(longer || view?.startupProgressUnavailable === true || view?.mode === 'start_failed' || (view?.mode === 'failed' && view.serviceDown) || (!localDesktop && view?.serviceDown === true));
	const autoStartWaiting = $derived(localDesktop && !startFailed && (deferred || (longer && autoStartAttempted && !starting)));

	$effect(() => {
		if (view === null) {
			starting = false;
			startFailed = false;
			manualStartFailed = false;
			autoStartAttempted = false;
			deferred = false;
			pending = null;
			return;
		}
		if ((view.mode === 'start_failed' || (view.mode === 'failed' && view.serviceDown)) && !starting && !deferred) startFailed = true;
		if (!onShell && localDesktop && (longer || startFailed)) {
			handOver();
			return;
		}
		if (longer && localDesktop && !autoStartAttempted) {
			autoStartAttempted = true;
			void startCorsia('automatic');
		}
	});
	// A deferred start did nothing, so it is asked again as the screen recomputes, at most once a second, as the start it
	// was: a press stays the press it answers, and the shell decides whether it is still allowed. It lives only in this
	// page, so a reload or Back never replays it.
	$effect(() => {
		if (view) untrack(() => { if (deferred && pending && !starting && Date.now() - askedAt >= 1_000) void startCorsia(pending); });
	});
	// The shell's progress read says the service answers again with no update holding it, or a reopened app's own launch
	// start answered Ready: back to its page, as on Ready.
	$effect(() => {
		const readyUrl = $startupProgress?.readyUrl ?? $launchReadyUrl;
		if (onShell && view && readyUrl) untrack(() => { if (!starting) returnTo(readyUrl); });
	});
	// On this computer's own service page the latest screen also stays in the address, for an app the update reopens to
	// carry; it is taken out once the screen ends. Presentation only.
	$effect(() => {
		if (onShell || !localDesktop) return;
		const showing = view !== null;
		untrack(() => keepScreenInAddress(showing ? handOverScreen(currentTheme(), `${window.location.pathname}${window.location.search}`) : null));
	});
	$effect(() => {
		if (recovery && localDesktop && startFailed) void tick().then(() => startActionEl?.focus());
	});
	$effect(() => {
		if (screenEl) screenEl.focus({ preventScroll: true });
	});

	/** Move the screen, as it stands, to the shell's update page. */
	function handOver(again = false): void {
		if (handedOver && !again) return;
		const screen = handOverScreen(currentTheme(), `${window.location.pathname}${window.location.search}`);
		if (!screen) return;
		handedOver = true;
		moveWindow(`${SHELL_UPDATE_PAGE}#${encodeHandoff(screen)}`);
	}

	/** Back to the service's page, which confirms the new version itself; nothing here calls it done. */
	function returnTo(url: string): boolean {
		if (returned) return true;
		const screen = handOverScreen(currentTheme(), keptReturnRoute());
		const target = screen && returnUrl(url, screen);
		if (!target) return false;
		returned = true;
		moveWindow(target);
		return true;
	}

	async function startCorsia(intent: RecoveryIntent): Promise<void> {
		askedAt = Date.now();
		starting = true;
		startFailed = false;
		manualStartFailed = false;
		const answer = await recoverLocalService(intent);
		starting = false;
		deferred = answer.recovery === 'deferred';
		pending = deferred ? intent : null;
		if (deferred) return;
		if (answer.recovery === 'ready' && returnTo(answer.url)) return;
		startFailed = true;
		manualStartFailed = intent === 'manual';
		if (intent === 'manual') autoStartAttempted = true;
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
						{#if view.startupProgressUnavailable}
							<p class="detail" role="alert">Couldn’t read startup progress. If CORSIA doesn’t return, quit CORSIA with ⌘Q and open it again.</p>
						{:else if autoStartWaiting}
							<p class="detail">It’s taking longer than usual. CORSIA is still starting. Nothing you need to do yet.</p>
						{:else if manualStartFailed}
							<p class="detail" role="alert">It still didn’t start. Click Start CORSIA to try again, or quit CORSIA with ⌘Q and open it again.</p>
						{:else if startFailed}
							<p class="detail">It didn’t start on its own.</p>
						{/if}
						{#if startFailed && !view.startupProgressUnavailable}
							<div class="actions" in:actionsReveal={{ duration: ms('--dur-tight') }}>
								<Button variant="primary" onclick={() => (onShell ? void startCorsia('manual') : handOver(true))} loading={starting} bind:el={startActionEl}>Start CORSIA</Button>
							</div>
						{/if}
					{:else if hostName}
						<p class="detail">Open CORSIA on {hostName}.</p>
						<p class="detail">Progress shows on your main computer, {hostName}.</p>
					{:else}
						<p class="detail">Open CORSIA on your main computer.</p>
						<p class="detail">Progress shows on your main computer.</p>
					{/if}
				{:else}
					<p class="detail">CORSIA comes back by itself. You can keep this window open.</p>
					{#if view.estimated}<p class="detail">Progress shows on your main computer{hostName ? `, ${hostName}` : ''}.</p>{/if}
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.update-screen { position: fixed; inset: 0; z-index: 10000; display: grid; place-items: center; padding: var(--space-5); box-sizing: border-box; overflow-y: auto; background: var(--bg-primary); color: var(--text-primary); outline: none; }
	.column { display: flex; flex-direction: column; align-items: center; min-width: 0; width: min(100%, calc(var(--space-8) * 8 + var(--space-7) + var(--space-1))); text-align: center; }
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
