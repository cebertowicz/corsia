<script lang="ts">
	import { onMount, tick, untrack } from 'svelte';
	import { Button, Toggle } from '$lib/components/ui';
	import { createPush, THIS_DEVICE, type ListedDevice } from '$lib/push.svelte';
	import { formatMoment, parseUTC } from '$lib/time';
	import SettingsConfirm from './SettingsConfirm.svelte';
	import SettingsGroup from './SettingsGroup.svelte';
	import SettingsDeviceName from './SettingsDeviceName.svelte';
	import SettingsReveal from './SettingsReveal.svelte';
	import SettingsRow from './SettingsRow.svelte';
	import SettingsStateLine from './SettingsStateLine.svelte';

	/**
	 * Settings › System, under Let other devices connect: each of the person's devices and whether it gets urgent
	 * notifications. The switch acts on this device only, so it works where the connections row above is read-only.
	 * Why the list is here: a lost, sold or shared device keeps getting urgent notifications, and this is the one place to
	 * stop that without holding that device (APP-MAP D8).
	 */
	const push = createPush();
	onMount(() => {
		void push.read();
		return push.watch();
	});

	const view = $derived(push.view);
	const hasSwitch = $derived(['loading', 'off', 'enabling', 'on', 'turned-off'].includes(view.thisDevice));
	/** R11: whoever delivers it sees which device it goes to and when, never what it says. One string for every state. */
	const HINT = 'For what can’t wait. Your browser’s maker delivers it and sees which device and when, never what it says.';

	const thisBusy = $derived(view.busy[THIS_DEVICE]);
	const thisProblem = $derived(view.problems[THIS_DEVICE]);
	/** This device says nothing while its switch shows the state; a line only for an exception. */
	const thisLine = $derived.by((): string => {
		if (thisProblem) return thisProblem;
		if (thisBusy) return 'Turning off…';
		switch (view.thisDevice) {
			case 'enabling': return 'Waiting for your permission…';
			case 'turned-off': return 'Stopped on its own. Turn on.';
			case 'denied': return view.platform === 'ios'
				? 'Blocked. Allow CORSIA in Settings › Notifications.'
				: 'Blocked in your browser’s settings. Allow CORSIA there.';
			case 'needs-home-screen': return 'Works in the Home Screen app only. Add CORSIA there.';
			case 'unsupported': return view.inDesktopApp
				? 'Not available in this app. Use CORSIA in a browser.'
				: 'Not available in this browser. Try another one.';
			default: return '';
		}
	});
	const thisTone = $derived(thisProblem ? 'failed' : view.thisDevice === 'denied' || view.thisDevice === 'turned-off' ? 'attention' : 'quiet');

	/** Two devices of one kind read alike, so every other device says when it was added. */
	const added = (device: ListedDevice) => {
		const text = device.added_at ? formatMoment(parseUTC(device.added_at), new Date(), { dateOnly: true }) : '';
		return text ? `Added ${text}` : '';
	};
	/** The date joins every action's accessible name, always, so two devices of one kind never share an accessible name. */
	const when = (device: ListedDevice) => {
		const text = added(device);
		return text ? `, ${text.charAt(0).toLowerCase()}${text.slice(1)}` : '';
	};
	/** One line per other device: its state and the date it was added. A stopped device says what to do next in a second line. */
	const otherLine = (device: ListedDevice) => {
		const busy = view.busy[device.id];
		if (busy) return busy === 'removing' ? 'Removing…' : 'Turning off…';
		return [device.state === 'turned_off' ? 'Stopped on its own' : 'On', added(device)].filter(Boolean).join(' · ');
	};
	const otherNote = (device: ListedDevice) => (device.state === 'turned_off' && !view.busy[device.id] ? 'Turn it on from that device.' : '');

	/** Keyboard focus is never left on something that is gone: it follows the person's action (F1). */
	let switchBox = $state<HTMLElement | undefined>();
	const triggers = $state<Record<string, HTMLButtonElement | undefined>>({});
	const cancels = $state<Record<string, HTMLButtonElement | undefined>>({});
	let confirming = $state<string | null>(null);
	const focusSwitch = async () => { await tick(); switchBox?.querySelector('button')?.focus(); };
	const stillHere = (el: Element | undefined | null) => document.activeElement === el || document.activeElement === document.body || !document.activeElement;

	async function ask(id: string) { confirming = id; await tick(); cancels[id]?.focus(); }
	async function cancelAsk(id: string) { confirming = null; await tick(); triggers[id]?.focus(); }
	async function confirm(id: string) {
		const ids = view.others.map((d) => d.id);
		const next = ids[ids.indexOf(id) + 1];
		confirming = null;
		await tick();
		triggers[id]?.focus();
		await push.remove(id);
		await tick();
		if (!stillHere(triggers[id])) return;
		const failed = push.view.others.some((d) => d.id === id);
		const target = failed ? triggers[id] : next ? triggers[next] : undefined;
		if (target) target.focus(); else await focusSwitch();
	}
	/** F2: Escape on an open confirm cancels it, as Cancel does. */
	function onKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape' || !confirming) return;
		event.preventDefault();
		event.stopPropagation();
		void cancelAsk(confirming);
	}
	/** F3: a row that leaves while its confirm is open takes the confirm with it; focus goes to the next row's action, else the switch. */
	let lastIds: string[] = [];
	$effect(() => {
		const ids = view.others.map((d) => d.id);
		const before = lastIds;
		lastIds = ids;
		const open = untrack(() => confirming);
		if (!open || ids.includes(open)) return;
		confirming = null;
		const next = before.slice(before.indexOf(open) + 1).find((id) => ids.includes(id)) ?? ids[0];
		void tick().then(() => { const target = next ? triggers[next] : undefined; if (target) target.focus(); else void focusSwitch(); });
	});
	async function cancelWaiting() { push.cancel(); await focusSwitch(); }
	const turnOffLabel = (device: ListedDevice) => `Turn off urgent notifications on ${device.name}${when(device)}`;
	const removeLabel = (device: ListedDevice) => `Remove ${device.name}${when(device)}`;
</script>

<SettingsGroup label="Urgent notifications" hint={HINT}>
<div class="urgent" role="presentation" onkeydown={onKeydown}>
	<SettingsRow name="This device">
		{#snippet actions()}
			{#if hasSwitch}
				<span class="switch" bind:this={switchBox}>
					<Toggle
						touch
						checked={view.thisDevice === 'on'}
						onchange={(next) => void (next ? push.enable() : push.disable())}
						ariaLabel="Urgent notifications on this device"
						disabled={view.thisDevice === 'loading'}
						locked={view.thisDevice === 'enabling' || !!thisBusy}
					/>
				</span>
			{:else}
				<span class="off">Off</span>
			{/if}
		{/snippet}
	</SettingsRow>
	<SettingsStateLine text={thisLine} tone={thisTone} alert={!!thisProblem}>
		{#snippet action()}
			{#if view.thisDevice === 'enabling' && !thisProblem}
				<Button variant="ghost" size="sm" onclick={() => void cancelWaiting()}>Cancel</Button>
			{/if}
		{/snippet}
	</SettingsStateLine>
	{#each view.others as device (device.id)}
		{@const problem = view.problems[device.id]}
		{@const busy = view.busy[device.id]}
		{@const turnOff = device.state === 'on'}
		{@const asking = confirming === device.id}
		{#snippet deviceName()}
			<SettingsDeviceName name={device.name} />
		{/snippet}
		<SettingsRow
			confirming={asking}
			name={deviceName}
			ariaLabel={device.name}
			sub={otherLine(device)}
			note={otherNote(device)}
			subTone={busy || turnOff ? 'quiet' : 'attention'}
			wrap
		>
			{#snippet actions()}
				{#if asking}
					<Button variant="ghost" size="sm" ariaLabel={turnOff ? turnOffLabel(device) : removeLabel(device)} onclick={() => void confirm(device.id)}>{turnOff ? 'Turn off' : 'Remove'}</Button>
					<Button variant="ghost" size="sm" bind:el={cancels[device.id]} ariaLabel={`Cancel ${turnOff ? 'turning off urgent notifications on' : 'removing'} ${device.name}${when(device)}`} onclick={() => void cancelAsk(device.id)}>Cancel</Button>
				{:else}
					<Button
						variant="ghost"
						size="sm"
						bind:el={triggers[device.id]}
						ariaDisabled={!!busy}
						ariaLabel={turnOff ? turnOffLabel(device) : removeLabel(device)}
						onclick={() => { if (!busy) void ask(device.id); }}
					>{turnOff ? 'Turn off' : 'Remove'}</Button>
				{/if}
			{/snippet}
		</SettingsRow>
		<SettingsReveal open={asking}>
			<SettingsConfirm
				question={turnOff ? `Turn off urgent notifications on ${device.name}?` : `Remove ${device.name}?`}
				detail={turnOff ? 'It stops getting them until you turn them on from that device.' : 'It leaves this list. Turn it on from that device to bring it back.'}
			/>
		</SettingsReveal>
		<SettingsStateLine text={problem ?? ''} tone="failed" alert />
	{/each}
</div>
</SettingsGroup>

<style>
	/* One wrapper, so the keydown for Escape sees the whole group; its rows stay the group's own flex items. */
	.urgent { display: contents; }
	/* F4: the text actions draw the switch's focus ring (accent, 2px, offset 2px), not the app-wide default. */
	.urgent :global(.btn:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
	/* F9: where the switch would be, the word Off, so a row that cannot be turned on is not silent. */
	.off { font-size: var(--text-caption); color: var(--text-secondary); padding-right: var(--space-2); }
	/* The switch's hit area is the touch target; it reaches past the track into the row's own padding, not into a neighbour. */
	.switch { --toggle-hit: var(--touch-target); display: inline-flex; }
	.switch :global(.tg.touch) { margin-inline-end: 0; }
</style>
