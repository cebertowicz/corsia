<script lang="ts">
	import Popover from '$lib/components/ui/Popover.svelte';
	import AgentOptions from '$lib/components/reviews/AgentOptions.svelte';
	import { agentOptionIndex } from '$lib/components/reviews/agentOptionKeys';
	import { changeOwnership, OwnershipChangeError, type DocOwnership } from '$lib/api/docOwnership';

	let { artifactId, ownership, onChange, saving = $bindable(false), error = $bindable(''), errorFor = $bindable(null), pending = $bindable(null), open = $bindable(false), externalAnchor = null }: { artifactId: string; ownership: DocOwnership; onChange: (value: DocOwnership) => void; saving?: boolean; error?: string; errorFor?: 'owner' | 'supervisor' | null; pending?: { kind: 'owner' | 'supervisor'; id: string; name: string } | null; open?: boolean; externalAnchor?: HTMLButtonElement | null } = $props();
	let ownAnchor = $state<HTMLButtonElement>();
	const anchor = $derived(externalAnchor ?? ownAnchor ?? null);
	let picker = $state<'owner' | 'supervisor' | null>(null);
	let search = $state('');
	let typing = $state(false);
	let active = $state(0);
	const ownerLabel = $derived(pending?.kind === 'owner' ? pending.name : ownership.owner.kind === 'person' ? 'You' : ownership.owner.kind === 'none' ? 'none' : ownership.owner.name ?? 'none');
	const supervisorLabel = $derived(pending?.kind === 'supervisor' ? pending.name : ownership.can_act ? ownership.supervisor?.name ?? 'none' : 'No one gets comments on this document.');
	const sameAgent = $derived(ownership.can_act && (pending?.kind === 'owner' ? pending.id : ownership.owner.kind === 'agent' ? ownership.owner.id : null) === (pending?.kind === 'supervisor' ? pending.id : ownership.supervisor?.id) && !!(pending?.kind === 'supervisor' ? pending.id : ownership.supervisor?.id));
	const agents = $derived(ownership.choices.map((choice) => ({ id: choice.id, name: choice.name, responsibility: choice.status === 'paused' ? 'Paused' : choice.status === 'usage_held' ? 'Usage held' : '' })));
	const matches = $derived((picker === 'owner' && !ownership.multi_person ? [{ id: 'user', name: 'You', responsibility: '' }, ...agents] : agents).filter((choice) => choice.name.toLowerCase().includes(search.trim().toLowerCase())));

	function show(which: 'owner' | 'supervisor', input: HTMLInputElement) {
		picker = which; search = ''; typing = false;
		const selected = which === 'owner' ? (ownership.owner.kind === 'person' ? 'user' : ownership.owner.id) : ownership.supervisor?.id;
		const options = which === 'owner' && !ownership.multi_person ? [{ id: 'user' }, ...agents] : agents;
		active = Math.max(0, options.findIndex((choice) => choice.id === selected));
		input.select();
	}
	function keys(event: KeyboardEvent) {
		if (event.key === 'Escape') { picker = null; return; }
		if (event.key === 'Enter' && matches.length) { event.preventDefault(); void choose(matches[Math.min(active, matches.length - 1)]); return; }
		const index = agentOptionIndex(event.key, active, matches.length);
		if (index !== undefined && index !== null) { event.preventDefault(); active = index; }
	}
	async function choose(choice: { id: string; name: string }) {
		const which = picker;
		if (!which) return;
		// The selected input becomes disabled while saving. Keep focus inside the
		// surface so Escape closes it instead of navigating away from the document.
		const dialog = document.activeElement?.closest<HTMLElement>('[role="dialog"]');
		if (dialog) { dialog.tabIndex = -1; dialog.focus(); }
		picker = null;
		pending = { kind: which, ...choice };
		saving = true;
		error = '';
		errorFor = null;
		try {
			const owner = which === 'owner' ? choice.id : ownership.owner.kind === 'person' ? 'user' : ownership.owner.id ?? 'user';
			const supervisor = which === 'supervisor' ? choice.id : null;
			onChange(await changeOwnership(artifactId, owner, supervisor));
		} catch (cause) {
			errorFor = which;
			error = cause instanceof OwnershipChangeError && cause.status === 409 && which === 'owner' && choice.id === 'user'
				? 'Taking ownership isn’t available when more than one person uses this CORSIA. Choose an agent instead.'
				: !navigator.onLine ? `Couldn’t change the ${which} because you’re offline. Reconnect and try again.` : `Couldn’t change the ${which}. Try again.`;
		}
		finally { pending = null; saving = false; }
	}
</script>

<button bind:this={ownAnchor} type="button" class="identity" aria-label={`Owner: ${ownerLabel}${pending?.kind === 'owner' ? ', saving' : error && errorFor === 'owner' ? ', change failed' : ''}; Supervisor: ${supervisorLabel}${pending?.kind === 'supervisor' ? ', saving' : error && errorFor === 'supervisor' ? ', change failed' : ''}`} aria-haspopup="dialog" aria-expanded={open} title={`Owner: ${ownerLabel} · Supervisor: ${supervisorLabel}`} onclick={() => { open = !open; }}>
	{#if sameAgent && !pending && !error}
		<span class="identity-part">Owner and supervisor: <span class="identity-name">{ownerLabel}</span></span>
	{:else}
		<span class="identity-part">Owner: <span class="identity-name" class:pending={pending?.kind === 'owner'}>{ownerLabel}</span>{#if pending?.kind === 'owner'}<span class="pending">&nbsp;Saving…</span>{:else if error && errorFor === 'owner'}<span class="failure">&nbsp;Change failed</span>{/if}</span>
		<span class="identity-part">Supervisor: <span class="identity-name" class:pending={pending?.kind === 'supervisor'}>{supervisorLabel}</span>{#if pending?.kind === 'supervisor'}<span class="pending">&nbsp;Saving…</span>{:else if error && errorFor === 'supervisor'}<span class="failure">&nbsp;Change failed</span>{/if}</span>
	{/if}
</button>
{#snippet fields()}
	<div class="owner-picker">
		<label for="owner-choice">Owner</label>
		<input class:pending={pending?.kind === 'owner'} id="owner-choice" aria-describedby={errorFor === 'owner' ? 'owner-error' : undefined} role="combobox" aria-autocomplete="list" aria-expanded={picker === 'owner'} aria-controls="owner-options" aria-activedescendant={picker === 'owner' && matches.length ? `owner-options-option-${matches[Math.min(active, matches.length - 1)].id}` : undefined} disabled={saving} value={picker === 'owner' && typing ? search : ownerLabel} onfocus={(event) => show('owner', event.currentTarget)} oninput={(event) => { typing = true; search = event.currentTarget.value; active = 0; }} onkeydown={keys} onblur={() => picker = null} />
		{#if picker === 'owner'}<AgentOptions id="owner-options" label="Owners" {matches} active={Math.min(active, matches.length - 1)} {choose} compact />{/if}
		{#if error && errorFor === 'owner'}<p id="owner-error" role="status">{error}</p>{/if}
		{#if pending?.kind === 'owner'}<span role="status">Saving…</span>{/if}
		<label for="supervisor-choice">Supervisor</label>
		<input class:pending={pending?.kind === 'supervisor'} id="supervisor-choice" aria-describedby={errorFor === 'supervisor' ? 'supervisor-error' : undefined} role="combobox" aria-autocomplete="list" aria-expanded={picker === 'supervisor'} aria-controls="supervisor-options" aria-activedescendant={picker === 'supervisor' && matches.length ? `supervisor-options-option-${matches[Math.min(active, matches.length - 1)].id}` : undefined} disabled={saving} value={picker === 'supervisor' && typing ? search : pending?.kind === 'supervisor' ? pending.name : ownership.supervisor?.name ?? 'No agents in this scope'} onfocus={(event) => show('supervisor', event.currentTarget)} oninput={(event) => { typing = true; search = event.currentTarget.value; active = 0; }} onkeydown={keys} onblur={() => picker = null} />
		{#if picker === 'supervisor'}<AgentOptions id="supervisor-options" label="Supervisors" {matches} active={Math.min(active, matches.length - 1)} {choose} compact />{/if}
		{#if error && errorFor === 'supervisor'}<p id="supervisor-error" role="status">{error}</p>{/if}
		{#if pending?.kind === 'supervisor'}<span role="status">Saving…</span>{/if}
	</div>
{/snippet}
<Popover {anchor} {open} initialFocus={errorFor === 'supervisor' ? '#supervisor-choice' : '#owner-choice'} placement="bottom" width="min(360px, calc(100vw - 16px))" onClose={() => { open = false; picker = null; }}>
	{@render fields()}
</Popover>

<style>
	.identity { display: flex; flex-wrap: wrap; column-gap: 0.5em; min-width: 0; max-width: 100%; overflow: hidden; text-align: left; border: 0; padding: 0; background: none; color: inherit; font: inherit; cursor: pointer; }
	.identity-part { display: flex; min-width: 0; max-width: 100%; white-space: pre; }
	.identity-name { min-width: min(10ch, 100%); overflow: hidden; text-overflow: ellipsis; }
	.identity:hover { color: var(--text-primary); }
	.pending { opacity: 0.65; }
	.identity-part > .pending, .identity-part > .failure { flex: none; }
	.failure { color: var(--red-text); }
	@media (any-pointer: coarse) { .identity { min-height: 44px; align-items: center; } }
	.identity:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.owner-picker { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-3); }
	label { color: var(--text-secondary); font-size: var(--text-footnote); }
	input { box-sizing: border-box; width: 100%; min-width: 0; min-height: var(--touch-target); padding: var(--space-2); color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius-lg); font: inherit; }
	p { margin: 0; color: var(--red-text); font-size: var(--text-footnote); }
</style>
