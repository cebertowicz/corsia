<script lang="ts">
	import Toggle from './ui/Toggle.svelte';
	import { token } from '$lib/stores/auth';
	import { activeThreadId, threads } from '$lib/stores/threads';
	import { saveThreadLocalOnly, threadLocalOnly } from '$lib/stores/threadLocalOnly';
	import { activeModelId, loadModels, modelMatchesId, models } from '$lib/stores/models';
	import { openProjectSettings } from '$lib/stores/settingsNavigation';
	import { openSubagentRequest } from '$lib/stores/subagents';

	// The Local only row at the top of the model sheet: the thread's switch, its cause when locked,
	// and why it can't be turned on. Every state and its copy are in the Local only design, §5.
	// onsetuplocal is passed only when nothing else in the sheet offers to add a local model.
	// offerssetup reports whether this row is showing that link, so the sheet adds no second one.
	let { onsetuplocal, onleave = () => {}, focusOnMount = false, onfocusconsumed = () => {}, offerssetup = $bindable(false) }: { offerssetup?: boolean; onsetuplocal?: () => void; onleave?: () => void; focusOnMount?: boolean; onfocusconsumed?: () => void } = $props();

	const thread = $derived($threads.find((row) => row.id === $activeThreadId));
	const policy = $derived(threadLocalOnly(thread));
	const cause = $derived(policy.cause);
	// A parent or project policy is held, distinct from a full-color in-flight save lock.
	const held = $derived(cause === 'project' || cause === 'inherited');
	let pending = $state<boolean | null>(null);
	const saving = $derived(pending !== null);
	const on = $derived(pending ?? cause !== 'off');
	let error = $state('');
	let rowEl = $state<HTMLDivElement>();

	const activeModel = $derived($models.find((model) => modelMatchesId(model, $activeModelId)) ?? null);
	const hasReadyLocalModel = $derived($models.some((model) => model.is_local === true && model.status === 'ready'));
	const needsLocalModel = $derived(!on && !hasReadyLocalModel);
	const needsModelSwitch = $derived(!on && hasReadyLocalModel && activeModel?.is_local !== true);
	// The one rule for this row's setup link; the template and the sheet both read it.
	const showsSetupLink = $derived(!!$activeThreadId && !!onsetuplocal && !held && needsLocalModel);
	$effect.pre(() => { offerssetup = showsSetupLink; });

	async function change(next: boolean) {
		const threadId = $activeThreadId;
		const auth = $token;
		if (!threadId || !auth || saving) return;
		error = '';
		pending = next;
		const result = await saveThreadLocalOnly(threadId, next, auth);
		pending = null;
		if (result.ok) await loadModels();
		else error = result.error;
	}

	function openParent() {
		if (policy.inheritedFromThreadId) openSubagentRequest.set(policy.inheritedFromThreadId);
		onleave();
	}
	function openProject() {
		if (thread?.project_id) openProjectSettings(thread.project_id, 'project-provider-policy');
		onleave();
	}

	// The request that opened this sheet carries the intent to focus this row,
	// including when the already-open sheet receives another request.
	$effect(() => {
		if (!focusOnMount) return;
		requestAnimationFrame(() => rowEl?.focus());
		onfocusconsumed();
	});
</script>

{#if $activeThreadId}
	<div class="sheet-section local-only-setting" tabindex="-1" bind:this={rowEl}>
		<div class="local-only-copy">
			<span class="local-only-label">Local only</span>
			<p id="local-only-description">
				{#if cause === 'inherited'}
					<button type="button" class="local-only-link" onclick={openParent}>Set by {policy.inheritedFromTitle ?? 'its parent chat'}</button>
				{:else if cause === 'project'}
					<button type="button" class="local-only-link" onclick={openProject}>Set by this project</button>
				{:else if needsLocalModel}
					No local model is ready.{#if showsSetupLink} <button type="button" class="local-only-link" onclick={onsetuplocal}>Set up a local model</button>{/if}
				{:else if needsModelSwitch}
					Switch this chat to a local model first.
				{:else if !on}
					When on, this chat and its agents use local models only.
				{:else}
					This chat and its agents use local models only.
				{/if}
			</p>
			{#if error}<p class="local-only-error" role="alert">{error}</p>{/if}
		</div>
		<div class="local-only-control">
			<Toggle checked={on} disabled={needsLocalModel || needsModelSwitch} held={held} locked={saving} ariaLabel="Local only" describedBy="local-only-description" onchange={change} />
			{#if saving}<span class="local-only-saving" aria-live="polite">Saving…</span>{/if}
		</div>
	</div>
{/if}

<style>
	.local-only-setting { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-3); }
	.local-only-setting:focus { outline: 2px solid var(--accent); outline-offset: -2px; border-radius: var(--radius-sm); }
	.local-only-copy { min-width: 0; }
	.local-only-label { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	.local-only-copy p { margin: var(--space-1) 0 0; color: var(--text-secondary); font-size: var(--text-footnote); overflow-wrap: anywhere; }
	.local-only-copy .local-only-error { color: var(--red-text); }
	.local-only-link { padding: 0; border: 0; background: transparent; color: var(--accent-text); font: inherit; text-align: left; text-decoration: underline; cursor: pointer; }
	.local-only-control { display: flex; flex: 0 0 auto; align-items: center; gap: var(--space-2); }
	.local-only-control :global(.tg) { flex: 0 0 auto; margin-top: var(--space-1); }
	.local-only-saving { font-size: var(--text-caption); color: var(--accent-text); }
</style>
