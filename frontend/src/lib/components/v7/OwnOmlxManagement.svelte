<script lang="ts">
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { token } from '$lib/stores/auth';
	import { Button } from '$lib/components/ui';
	import ConfirmDialog from '../ConfirmDialog.svelte';
	import { changeOwnOmlxManagement, fetchSources, type OwnOmlxManagement } from '$lib/conductorSources';
	import { MANAGE_DISCLOSURE, MANAGE_DISCLOSURE_LEAD, MANAGE_LABEL, MANAGING, RESTORE_FAILED, RESTORED, STOP_MANAGING, STOP_TITLE, stopDescription } from '$lib/ownOmlxCopy';

	/**
	 * Settings' one control for the person's own oMLX (J9): Stop managing puts back every value
	 * CORSIA replaced; Let CORSIA manage it gives Welcome's consent. Nothing shows for the oMLX
	 * CORSIA installed. Results go to the modal's message line through `onResult`.
	 */
	interface Props { onResult: (message: string, error: boolean) => void; }
	const { onResult }: Props = $props();

	let management = $state<OwnOmlxManagement | null>(null);
	let confirming = $state(false);
	let busy = $state(false);

	async function read() {
		try {
			const sources = await fetchSources(get(token) || '');
			management = sources.find((source) => source.source === 'omlx')?.omlx_management ?? null;
		} catch {
			management = null;
		}
	}

	async function change(action: 'start' | 'stop') {
		confirming = false;
		busy = true;
		try {
			const result = await changeOwnOmlxManagement(get(token) || '', action);
			if (result.ok) onResult(action === 'stop' ? RESTORED : MANAGING, false);
			else onResult(result.message || (action === 'stop' ? RESTORE_FAILED : 'CORSIA couldn’t start managing your oMLX. Try again.'), true);
		} catch {
			onResult(action === 'stop' ? RESTORE_FAILED : 'CORSIA couldn’t start managing your oMLX. Try again.', true);
		} finally {
			busy = false;
			await read();
		}
	}

	onMount(() => { void read(); });
</script>

{#if management?.state === 'managed'}
	<Button disabled={busy} onclick={() => (confirming = true)}>{STOP_MANAGING}</Button>
	<ConfirmDialog open={confirming} title={STOP_TITLE} description={stopDescription(management.restores, management.forgets_key)}
		confirmLabel={STOP_MANAGING} onConfirm={() => void change('stop')} onCancel={() => (confirming = false)} />
{:else if management?.state === 'restore_failed'}
	<span class="failed" role="status">{RESTORE_FAILED}</span>
	<Button disabled={busy} loading={busy} onclick={() => void change('stop')}>Try again</Button>
{:else if management?.state === 'not_managed'}
	<Button disabled={busy} onclick={() => (confirming = true)}>{MANAGE_LABEL}</Button>
	<ConfirmDialog open={confirming} title="{MANAGE_LABEL}?" description="{MANAGE_DISCLOSURE_LEAD} {MANAGE_DISCLOSURE}"
		confirmLabel={MANAGE_LABEL} onConfirm={() => void change('start')} onCancel={() => (confirming = false)} />
{/if}

<style>
	.failed { font-size: var(--text-footnote); color: var(--text-secondary); }
</style>
