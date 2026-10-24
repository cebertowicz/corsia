<script lang="ts">
	import { accountAddress, clippedPrefix, localSegments, providerIsDomain, width } from '$lib/accountAddress';
	import type { ConnectedAccount } from '$lib/api/connectedApps';
	/**
	 * A closed account row's title on one line: "Provider — you@domain", or the address alone when the provider is only
	 * the address's domain (a typed-in host). What gives way when it is too long, in order: the domain (kept to a stub
	 * with the "@", ending in an ellipsis), then the provider, and only then the local part, which stays distinct from
	 * the other accounts on the same domain.
	 */
	let { account, peers }: { account: ConnectedAccount; peers: ConnectedAccount[] } = $props();
	let prefix = $state<string | null>(null);
	// A long local-part suffix is dropped so the provider label and the domain keep their room.
	let dropSuffix = $state(false);
	const parts = $derived(accountAddress(account.address));
	const addressOnly = $derived(providerIsDomain(account.provider_display_name, account.address));
	const shownPrefix = $derived(parts ? prefix ?? parts.localPrefix : account.address);
	const shownSuffix = $derived.by(() => {
		if (!parts || dropSuffix) return '';
		return shownPrefix.endsWith('…') && /^[._+\-]/.test(parts.localSuffix) ? parts.localSuffix.slice(1) : parts.localSuffix;
	});
	function distinctPrefix(input: { account: ConnectedAccount; peers: ConnectedAccount[] }, available: number, style: CSSStyleDeclaration): string {
		const address = accountAddress(input.account.address);
		if (!address || width(address.localPrefix, style) <= available) return address?.localPrefix ?? input.account.address;
		const ownSegments = localSegments(address.localPrefix);
		const siblings = input.peers.filter((candidate) => {
			const other = accountAddress(candidate.address);
			return candidate.id !== input.account.id && other?.domain === address.domain && localSegments(other.localPrefix)[0] === ownSegments[0];
		});
		const distinctIndex = ownSegments.findIndex((segment, index) => siblings.every((candidate) => localSegments(accountAddress(candidate.address)!.localPrefix)[index] !== segment));
		if (distinctIndex <= 0) return clippedPrefix(address.localPrefix, available, style);
		const distinctive = ownSegments[distinctIndex];
		const lead = ownSegments[0];
		const candidate = `${lead}…${distinctive}`;
		if (width(candidate, style) <= available) return candidate;
		for (let length = [...lead].length - 1; length > 0; length--) {
			const shortened = `${[...lead].slice(0, length).join('')}…${distinctive}`;
			if (width(shortened, style) <= available) return shortened;
		}
		return clippedPrefix(address.localPrefix, available, style);
	}
	function fit(node: HTMLElement, input: { account: ConnectedAccount; peers: ConnectedAccount[] }) {
		let current = input;
		let frame = 0;
		let lastKey = '';
		const natural = (el: HTMLElement) => Math.max(el.getBoundingClientRect().width, el.scrollWidth);
		const update = () => {
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(() => {
				const address = accountAddress(current.account.address);
				const prefixEl = node.querySelector<HTMLElement>('.account-local-prefix');
				const providerEl = node.querySelector<HTMLElement>('.account-provider');
				const suffixEl = node.querySelector<HTMLElement>('.account-local-suffix');
				const domainEl = node.querySelector<HTMLElement>('.account-domain');
				// A title that isn't laid out (hidden, or not yet shown) has no room to measure: keep the whole address until it does.
				if (!address || !prefixEl || !suffixEl || !domainEl || !node.clientWidth) return;
				const key = [node.clientWidth, current.account.address, current.peers.map((peer) => peer.address).join()].join('|');
				if (key === lastKey) return;
				lastKey = key;
				const prefixStyle = getComputedStyle(prefixEl);
				const providerWidth = providerEl ? natural(providerEl) + (Number.parseFloat(getComputedStyle(providerEl).marginInlineEnd) || 0) : 0;
				// An address-only title keeps its whole local part: the domain may shrink to a stub, so only the stub is reserved.
				const domainWidth = providerEl ? natural(domainEl) : Math.min(natural(domainEl), width(`@${[...address.domain].slice(0, 8).join('')}…`, getComputedStyle(domainEl)));
				const base = node.clientWidth - providerWidth - domainWidth;
				const withSuffix = Math.max(0, base - width(address.localSuffix, getComputedStyle(suffixEl)));
				// With too little room left for even the start of the name, the suffix goes first.
				const dropped = address.localSuffix !== '' && withSuffix < width(`${[...address.localPrefix].slice(0, 4).join('')}…`, prefixStyle);
				const available = dropped ? Math.max(0, base) : withSuffix;
				dropSuffix = dropped;
				prefix = distinctPrefix(current, available, prefixStyle);
				requestAnimationFrame(() => {
					const overflow = node.scrollWidth - node.clientWidth;
					if (overflow > 0.5) prefix = distinctPrefix(current, Math.max(0, available - overflow - 1), prefixStyle);
				});
			});
		};
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(node); update();
		return { update(next: { account: ConnectedAccount; peers: ConnectedAccount[] }) { current = next; update(); }, destroy() { cancelAnimationFrame(frame); observer?.disconnect(); } };
	}
</script>

<span class="account-closed-name" use:fit={{ account, peers }}>{#if !addressOnly}<span class="account-provider">{account.provider_display_name} —</span>{/if}<span class="account-local-prefix">{shownPrefix}</span><span class="account-local-suffix">{shownSuffix}</span><span class="account-domain">@{parts?.domain}</span></span>

<style>
	.account-closed-name { display: flex; min-width: 0; overflow: hidden; white-space: nowrap; font-size: var(--text-subhead); color: var(--text-primary); }
	.account-provider, .account-local-prefix, .account-local-suffix, .account-domain { flex: 0 0 auto; }
	/* When provider and domain alone outgrow the row, the domain gives way first, then the provider, each ending in an ellipsis. */
	.account-provider { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; margin-inline-end: var(--space-1); }
	.account-domain { flex: 0 1000 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
	.account-local-prefix { overflow: hidden; text-overflow: clip; }
</style>
