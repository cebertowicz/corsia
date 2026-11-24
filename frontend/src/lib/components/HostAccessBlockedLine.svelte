<script lang="ts">
	import { Button } from '$lib/components/ui';

	/**
	 * What a request drawer's footer says above its decision: the server's refusal of the last decision,
	 * and why Approve is off. One red line each, no box. The fill, script and lease request drawers and the
	 * active drawer all draw it from here, so its words and look live in one place.
	 */
	let {
		stale = false,
		incomplete = false,
		kind = 'lease',
		rejectable = true,
		notice = null,
		onRetry,
	}: {
		/** The last read failed: Retry reads again. */
		stale?: boolean;
		/** The request lacks what CORSIA needs to check it: no Retry, since reading again won't change it. */
		incomplete?: boolean;
		/** Which request this is: a script's refusal is Deny, and each kind clears in its own way. */
		kind?: 'lease' | 'fill' | 'script';
		/** Whether an incomplete request can still be rejected (its decision fields are whole). */
		rejectable?: boolean;
		/** The server's own line when it refused this request's last decision. */
		notice?: string | null;
		onRetry?: () => void;
	} = $props();

	/**
	 * Why an incomplete request can't be approved, and what the person can do instead. When its decision
	 * fields are missing it can't be refused either, so the line says how that kind of request clears.
	 */
	function incompleteLine(requestKind: 'lease' | 'fill' | 'script', canRefuse: boolean): string {
		if (requestKind === 'script') {
			return canRefuse
				? 'This request is missing details CORSIA needs to check it, so you can’t approve it. Deny it and the agent can ask again.'
				: 'This request is missing details CORSIA needs to check it, so you can’t approve or deny it. It stays here until the agent withdraws it or the agent is stopped.';
		}
		if (canRefuse) {
			return 'This request is missing details CORSIA needs to check it, so you can’t approve it. Reject it and the agent can ask again.';
		}
		return requestKind === 'fill'
			? 'This request is missing details CORSIA needs to check it, so you can’t approve or reject it. It clears when CORSIA restarts.'
			: 'This request is missing details CORSIA needs to check it, so you can’t approve or reject it. It stays here until the agent withdraws it or CORSIA restarts.';
	}
</script>

{#if notice}
	<p class="blocked-line decision-notice" role="alert">{notice}</p>
{/if}
{#if stale}
	<div class="blocked-line" role="status">
		<span>Couldn’t check that this request is still current, so you can’t approve it.</span>
		{#if onRetry}<Button variant="ghost" size="sm" onclick={onRetry}>Retry</Button>{/if}
	</div>
{:else if incomplete}
	<p class="blocked-line" role="status">{incompleteLine(kind, rejectable)}</p>
{/if}

<style>
	.blocked-line {
		display: flex;
		align-items: center;
		gap: var(--space-2);
		margin: 0;
		color: var(--red-text);
		font-size: var(--text-footnote);
	}
	.blocked-line span { flex: 1; min-width: 0; }
	/* A ghost action's text sits inside its padding and border; pull it out so the word lines up
	   with the right edge of Approve below it. */
	.blocked-line :global(.btn.ghost) { margin-right: calc(-1 * (var(--button-pad-x-sm) + 1px)); }
</style>
