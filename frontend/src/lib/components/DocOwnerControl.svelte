<script lang="ts">
	import Popover, { anchorClearing } from '$lib/components/ui/Popover.svelte';
	import AgentOptions from '$lib/components/reviews/AgentOptions.svelte';
	import MiddleName from '$lib/components/MiddleName.svelte';
	import { tick } from 'svelte';
	import { agentOptionIndex } from '$lib/components/reviews/agentOptionKeys';
	import { changeOwnership, type DocOwnership } from '$lib/api/docOwnership';

	type Role = 'owner' | 'supervisor';
	let { artifactId, ownership, onChange }: { artifactId: string; ownership: DocOwnership; onChange: (value: DocOwnership) => void } = $props();

	// Owner and Supervisor are two controls, each with its own picker; one picker is open at a time.
	let picker = $state<Role | null>(null);
	let anchor = $state<HTMLButtonElement | null>(null);
	let search = $state('');
	let active = $state(0);
	let saving = $state(false);
	let pending = $state<{ role: Role; id: string; name: string } | null>(null);
	let failed = $state<{ role: Role; message: string } | null>(null);
	let group = $state<HTMLElement>();
	// The picker opens below both items, so it never covers the other one when they wrap onto two lines.
	const pickerAnchor = $derived(anchorClearing(anchor, () => group));

	const ownerName = $derived(pending?.role === 'owner' ? pending.name : ownership.owner.kind === 'person' ? 'You' : ownership.owner.kind === 'none' ? 'Not set' : ownership.owner.name ?? 'Not set');
	const ownerId = $derived(pending?.role === 'owner' ? pending.id : ownership.owner.kind === 'person' ? 'user' : ownership.owner.id);
	// A paused supervisor keeps its name with the list's Paused mark; one that is gone reads "Not set".
	const supervisorId = $derived(pending?.role === 'supervisor' ? pending.id : ownership.supervisor?.id ?? null);
	const supervisorStatus = $derived(supervisorId ? (ownership.supervisor?.id === supervisorId ? ownership.supervisor.status : ownership.choices.find((choice) => choice.id === supervisorId)?.status) : undefined);
	const supervisorPaused = $derived(supervisorStatus === 'paused');
	const supervisorShown = $derived(!!supervisorId && (pending?.role === 'supervisor' || ownership.can_act || supervisorPaused));
	const supervisorName = $derived(pending?.role === 'supervisor' ? pending.name : supervisorShown ? ownership.supervisor?.name ?? 'Not set' : 'Not set');
	// One agent holding both roles reads as one item; its Owner picker can still hand the supervisor role to another agent.
	const merged = $derived(supervisorShown && !pending && !failed && ownership.owner.kind === 'agent' && ownerId === supervisorId);

	const agents = $derived(ownership.choices.map((choice) => ({ id: choice.id, name: choice.name, responsibility: choice.status === 'paused' ? 'Paused' : choice.status === 'usage_held' ? 'Usage held' : '' })));
	// "You" is offered only on a CORSIA one person uses; the server refuses it otherwise.
	const ownerChoices = $derived(ownership.multi_person ? agents : [{ id: 'user', name: 'You', responsibility: '' }, ...agents]);
	const options = $derived(picker === 'owner' ? ownerChoices : agents);
	const matches = $derived(options.filter((choice) => choice.name.toLowerCase().includes(search.trim().toLowerCase())));

	function open(role: Role, button: HTMLButtonElement) {
		if (picker === role) { picker = null; return; }
		anchor = button; picker = role; search = '';
		const list = role === 'owner' ? ownerChoices : agents;
		active = Math.max(0, list.findIndex((choice) => choice.id === (role === 'owner' ? ownerId : supervisorId)));
	}
	function close() { picker = null; }
	function restoreFocus() { void tick().then(() => (anchor?.isConnected ? anchor : null)?.focus()); }
	function keys(event: KeyboardEvent) {
		if (event.key === 'Enter' && matches.length) { event.preventDefault(); void choose(matches[Math.min(active, matches.length - 1)]); return; }
		const index = agentOptionIndex(event.key, active, matches.length);
		if (index !== undefined && index !== null) { event.preventDefault(); active = index; }
	}
	async function choose(choice: { id: string; name: string }) {
		const role = picker;
		if (!role || saving) return;
		picker = null;
		restoreFocus();
		if (choice.id === (role === 'owner' ? ownerId : supervisorId)) return;
		pending = { role, id: choice.id, name: choice.name };
		saving = true; failed = null;
		try {
			const owner = role === 'owner' ? choice.id : ownership.owner.kind === 'person' ? 'user' : ownership.owner.id ?? 'user';
			// A supervisor the person picked stays through an owner change (the server drops it if it equals the new owner); a default one stays unset.
			const supervisor = role === 'supervisor' ? choice.id : ownership.supervisor?.source === 'picked' ? ownership.supervisor.id : null;
			onChange(await changeOwnership(artifactId, owner, supervisor));
		} catch {
			failed = { role, message: !navigator.onLine ? `Couldn’t change the ${role} because you’re offline. Reconnect and try again.` : `Couldn’t change the ${role}. Try again.` };
		} finally { pending = null; saving = false; }
	}
	function saveNote(role: Role) { return pending?.role === role ? ', saving' : failed?.role === role ? ', change failed' : ''; }
	// The second Owner picker route: when one agent holds both roles, the merged item's picker offers the supervisor role separately.
	function pickSupervisor() { const button = anchor; picker = null; if (button) void tick().then(() => open('supervisor', button)); }
</script>

{#snippet status(role: Role)}
	{#if pending?.role === role}<span class="note-inline">Saving…</span>{:else if failed?.role === role}<span class="failure">Change failed</span>{/if}
{/snippet}
{#snippet chevron()}
	<svg class="chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6" /></svg>
{/snippet}

<span bind:this={group} class="ownership" role="group" aria-label="Owner and supervisor">
	{#if merged}
		<button type="button" class="item" data-role="owner" aria-label={`Owner and supervisor: ${ownerName}${supervisorPaused ? ', paused' : ''}`} aria-haspopup="dialog" aria-expanded={picker !== null} onclick={(event) => open('owner', event.currentTarget)}>
			<span class="label">Owner and supervisor:</span><span class="name"><MiddleName name={ownerName} /></span>{#if supervisorPaused}<span class="mark">Paused</span>{/if}{@render chevron()}
		</button>
	{:else}
		<button type="button" class="item" data-role="owner" aria-label={`Owner: ${ownerName}${saveNote('owner')}`} aria-haspopup="dialog" aria-expanded={picker === 'owner'} onclick={(event) => open('owner', event.currentTarget)}>
			<span class="label">Owner:</span><span class="name" class:pending={pending?.role === 'owner'}><MiddleName name={ownerName} /></span>{@render status('owner')}{@render chevron()}
		</button>
		<button type="button" class="item" data-role="supervisor" aria-label={`Supervisor: ${supervisorName}${supervisorPaused ? ', paused' : ''}${saveNote('supervisor')}`} aria-haspopup="dialog" aria-expanded={picker === 'supervisor'} onclick={(event) => open('supervisor', event.currentTarget)}>
			<span class="label">Supervisor:</span><span class="name" class:pending={pending?.role === 'supervisor'}><MiddleName name={supervisorName} /></span>{#if supervisorPaused}<span class="mark">Paused</span>{/if}{@render status('supervisor')}{@render chevron()}
		</button>
	{/if}
</span>

<Popover anchor={pickerAnchor} open={picker !== null} sideKey={picker} ariaLabel={picker === 'owner' ? 'Owner' : 'Supervisor'} initialFocus=".role-picker input" placement="bottom" width="min(320px, calc(100vw - 16px))" onClose={close} {restoreFocus} keepOpenOnPointer={(target) => target instanceof Node && !!group?.contains(target)}>
	{#if picker}
		{@const role = picker}
		<div class="role-picker" data-role={role}>
			<label for={`${role}-choice`}>{role === 'owner' ? 'Owner' : 'Supervisor'}</label>
			<input id={`${role}-choice`} role="combobox" aria-autocomplete="list" aria-expanded="true" aria-controls={`${role}-options`} aria-activedescendant={matches.length ? `${role}-options-option-${matches[Math.min(active, matches.length - 1)].id}` : undefined} aria-describedby={failed?.role === role ? `${role}-error` : undefined} placeholder="Search agents" autocomplete="off" value={search} oninput={(event) => { search = event.currentTarget.value; active = 0; }} onkeydown={keys} />
			{#if options.length}<AgentOptions id={`${role}-options`} label={role === 'owner' ? 'Owners' : 'Supervisors'} {matches} active={Math.min(active, matches.length - 1)} {choose} compact />
			{:else}<p class="note">There are no agents in this project to choose from.</p>{/if}
			{#if failed?.role === role}<p id={`${role}-error`} class="failure" role="status">{failed.message}</p>{/if}
			{#if role === 'owner' && ownership.multi_person}<p class="note">Only an agent can own a document when more than one person uses CORSIA here.</p>{/if}
			{#if role === 'supervisor' && supervisorPaused}<p class="note">This agent is paused. Comments wait until it resumes.</p>
			{:else if role === 'supervisor' && !supervisorShown}<p class="note">No one gets comments on this document.</p>{/if}
			{#if role === 'owner' && merged}<button type="button" class="link" onclick={pickSupervisor}>Choose a different supervisor</button>{/if}
		</div>
	{/if}
</Popover>

<style>
	/* Two items that wrap onto their own lines when the row is too narrow for both, instead of squeezing their names. */
	.ownership { display: flex; flex-wrap: wrap; align-items: center; column-gap: var(--space-2); row-gap: 0; min-width: 0; max-width: 100%; }
	/* Each item is a ghost button like the toolbar's (.doc-tool): quiet at rest, filled on hover, with a chevron that says it opens. */
	.item { display: inline-flex; align-items: center; gap: var(--space-1); min-width: 0; max-width: 100%; border: 0; padding: calc(var(--space-1) / 2) var(--space-2); margin-left: calc(var(--space-2) * -1); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font: inherit; text-align: left; cursor: pointer; white-space: nowrap; }
	.item + .item { margin-left: 0; }
	.item:hover, .item[aria-expanded='true'] { background: var(--bg-tertiary); color: var(--text-primary); }
	.item:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
	.label { flex: none; }
	.name { min-width: 0; display: flex; color: var(--text-primary); }
	.chevron { flex: none; opacity: 0.7; }
	.mark { flex: none; color: var(--text-tertiary); font-size: var(--text-caption); }
	.pending { opacity: 0.65; }
	.note-inline { flex: none; color: var(--text-tertiary); }
	.failure { flex: none; color: var(--red-text); }
	@media (any-pointer: coarse) { .item { min-height: var(--touch-target); } }
	.role-picker { display: flex; flex-direction: column; gap: var(--space-1); padding: var(--space-3); }
	label { color: var(--text-secondary); font-size: var(--text-footnote); }
	input { box-sizing: border-box; width: 100%; min-width: 0; min-height: var(--touch-target); padding: var(--space-2); color: var(--text-primary); background: var(--bg-primary); border: 1px solid var(--border); border-radius: var(--radius-lg); font: inherit; }
	p { margin: 0; font-size: var(--text-footnote); }
	p.note { color: var(--text-secondary); }
	.link { align-self: flex-start; min-height: var(--touch-target); border: 0; padding: 0; background: none; color: var(--accent); font: inherit; font-size: var(--text-footnote); cursor: pointer; }
</style>
