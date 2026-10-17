<script lang="ts">
	import { Button, CodeInput, StatePill, Toggle } from '$lib/components/ui';
	import type { DevicesView } from '$lib/devices.svelte';

	/**
	 * Settings › System, after Account: whether other computers on this network may connect to this one, and the code
	 * that pairs one. A row built like Focus mode's, with no heading of its own. This is the view of one state; the row
	 * Settings renders (SettingsDevices) feeds it from the server, and the setup screen's picture and the design frames
	 * feed it fixed states.
	 */
	interface Props {
		view: DevicesView;
		onToggle?: (next: boolean) => void;
		onNewCode?: () => void;
		onRetry?: () => void;
	}
	const { view, onToggle = () => {}, onNewCode = () => {}, onRetry = () => {} }: Props = $props();

	const NAME = 'Let other devices connect';
	const DESCRIPTION = 'Your other computers on this network can find this one and connect with a code. Use only on a network you trust. Turning this off disconnects them.';
	// "itself" stays with the name, so a long name never leaves "itself." alone on a line.
	const NBSP = ' ';

	// Only the first read dims the switch. A start in progress keeps the person's choice (on, full colour) and locks it;
	// seen from a connected computer, or when the state could not be read, there is no switch at all.
	const switchDimmed = $derived(view.load === 'loading');
	const locked = $derived(view.busy !== null);
	const description = $derived(view.load === 'loading' ? 'Loading…' : DESCRIPTION);
	// The addresses to type, as the host's own: IPv6 only when there is no IPv4 one (nobody types fd5c:9e1a:3b7d:1::1a).
	const typed = $derived.by(() => {
		const v6 = (a: string) => a.startsWith('[') || (a.match(/:/g) ?? []).length > 1;
		const v4 = view.addresses.some((a) => /^\d+\.\d+\.\d+\.\d+/.test(a));
		return view.addresses.filter((a) => !(v4 && v6(a)));
	});
	const endsIn = $derived((view.endsInSeconds ?? 0) < 60 ? 'Ends in under a minute' : `Ends in ${Math.ceil((view.endsInSeconds ?? 0) / 60)} min`);
	// A connected computer sees the state even when off, so it gets the status slot too.
	const showMore = $derived(view.load === 'ready' && (view.enabled || view.readOnly));
	const statusLabel = $derived(view.busy === 'starting' ? 'Starting…' : view.announced ? `Visible as ${view.hostName}` : null);
	/**
	 * Seen from a connected computer there is no switch, so the state takes the status slot: "Visible as {name}" (with its
	 * dot) says it is on; off, and on but not visible, are plain secondary text with no dot.
	 */
	const readOnlyText = $derived(view.readOnly ? (!view.enabled ? 'Off' : view.announced ? null : 'On') : null);
</script>

<div class="settings-card devices" data-testid="devices-row">
	<div class="row">
		<div class="info">
			<span class="name">{NAME}</span>
			<span class="desc">{description}</span>
			{#if view.readOnly}<span class="manage">Manage connections on <span class="nowrap">{view.hostName}{NBSP}itself.</span></span>{/if}
		</div>
		{#if !view.readOnly && view.load !== 'failed'}
			<Toggle checked={view.enabled} onchange={onToggle} ariaLabel={NAME} disabled={switchDimmed} {locked} />
		{/if}
	</div>

	{#if view.load === 'failed'}
		<div class="more reveal">
			<p class="problem">Couldn’t load device settings.</p>
			<div class="action"><Button variant="chip" size="sm" onclick={onRetry}>Try again</Button></div>
		</div>
	{:else if view.listener === 'all-ports-taken' && !view.readOnly}
		<!-- All ten ports CORSIA may use are held: the switch is back off, and the line names what to do. -->
		<div class="more reveal">
			<p class="problem">Couldn’t turn this on. Other apps are using the connections CORSIA needs. Restart this computer, then try again.</p>
			<div class="action"><Button variant="chip" size="sm" onclick={onRetry}>Try again</Button></div>
		</div>
	{:else if view.listener === 'unavailable' && !view.readOnly}
		<!-- Listening failed for a reason other than taken ports (credentials, permission): a restart would not help. -->
		<div class="more reveal">
			<p class="problem">Couldn’t turn this on. CORSIA couldn’t start listening on this computer. Try again, and if it keeps failing, restart CORSIA.</p>
			<div class="action"><Button variant="chip" size="sm" onclick={onRetry}>Try again</Button></div>
		</div>
	{:else if showMore}
		<div class="more reveal">
			{#if readOnlyText}
				<div class="status"><span class="plain" data-testid="devices-state">{readOnlyText}</span></div>
			{:else if statusLabel}
				<div class="status">{#key statusLabel}<span class="swap"><StatePill look="dot" wrap state={view.busy === 'starting' ? 'idle' : 'success'} label={statusLabel} /></span>{/key}</div>
			{/if}
			{#if view.readOnly || view.busy !== null}
				<!-- Nothing to do here: read-only shows the state, and a start in progress shows only its status. -->
			{:else if view.pairing === 'code'}
				<div class="code reveal">
					<CodeInput label="Pairing code" value={view.code ?? ''} readonly />
					<span class="ends">{endsIn}</span>
				</div>
			{:else if view.pairing === 'paused'}
				<p class="problem">Pairing is paused after too many wrong codes.</p>
			{:else if !view.announced}
				<p class="line">This computer isn’t visible on the network. Other computers can still connect by typing {#each typed as address, i}{#if i > 0}{i === typed.length - 1 ? ' or ' : ', '}{/if}<span class="addr">{address}</span>{/each}.</p>
			{/if}
			{#if !view.readOnly && view.busy === null}
				<div class="action"><Button variant="chip" size="sm" onclick={onNewCode}>New code</Button></div>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* The Focus mode row's build (a settings card, name and description left, the switch right, the switch under the text
	   on a phone), so the two read as one family on System. Everything under the row stays inside the card. */
	.settings-card { margin-bottom: var(--space-4); padding: var(--space-5) var(--space-6); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-secondary); }
	.row { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--space-4); }
	.info { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: calc(var(--space-1) * 0.75); }
	.name { color: var(--text-primary); font-size: var(--text-subhead); font-weight: var(--fw-medium); }
	@media (max-width: 48em) { .row { flex-direction: column; gap: calc(var(--space-1) * 2.5); } }
	.desc { color: var(--text-muted); font-size: var(--text-footnote); line-height: var(--lh-normal); text-wrap: pretty; }
	.manage { color: var(--text-secondary); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	/* The whole name and "itself." stay together on one line; they break only when the name alone is wider than the row. */
	.nowrap { white-space: nowrap; }
	@media (max-width: 48em) { .nowrap { white-space: normal; } }
	/* Everything under the row is set once, at the description's size and line height on every width: the status, the
	   problem and the countdown inherit it and only choose a colour. The code boxes keep their own size. */
	.more { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-3); margin-top: var(--space-5); font-size: var(--text-footnote); line-height: var(--lh-normal); }
	.status { display: flex; }
	.status :global(.dotmark) { font-size: inherit; line-height: inherit; color: var(--text-primary); }
	/* The dot sits on the first line's baseline and is lifted to the middle of its x-height, so it holds when a long name wraps. */
	.status :global(.dotmark.wrap) { align-items: first baseline; }
	.status :global(.dotmark.wrap .dot) { margin-top: 0; transform: translateY(calc(var(--space-1) * 0.75 - 0.5ex)); }
	/* Read-only, off: the state as secondary text in the status slot, with no dot. */
	.plain { color: var(--text-secondary); }
	.line, .problem { margin: 0; text-wrap: pretty; }
	.line { color: var(--text-secondary); }
	.problem { color: var(--red-text); min-width: 0; max-width: 100%; }
	.addr { font-family: var(--font-mono); overflow-wrap: anywhere; }
	/* The status slot's text changes in place (Starting… to Visible as) with a cross-fade. */
	.swap { animation: reveal var(--dur-standard) var(--ease-entrance) both; }
	.code { display: flex; align-items: center; flex-wrap: wrap; gap: var(--space-3) var(--space-4); }
	.ends { color: var(--text-secondary); }
	/* A text action sits on the text's left edge, as in setup's dock. */
	.action { margin-left: calc(var(--space-2) * -1); }

	.reveal { animation: reveal var(--dur-standard) var(--ease-entrance) both; }
	@keyframes reveal { from { opacity: 0; } }
	@media (prefers-reduced-motion: reduce) { .reveal, .swap { animation: none; } }
</style>
