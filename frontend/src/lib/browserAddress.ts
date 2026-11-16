/**
 * The visible address leads with the page's registrable domain and only elides the middle.
 *
 * The backend names that domain from the Public Suffix List (host_access/browser_domain.py), the same
 * one the password step's title uses, so the pane never guesses where one site ends and another starts.
 * Without it, the host's own last labels lead (see hostLabelParts), so a field that cuts the string still
 * cuts it in front of the part that names the site, never after it.
 */
export function browserAddressLabel(raw: string, domain?: string | null): string {
	try {
		const parsed = new URL(raw);
		const host = parsed.hostname.toLowerCase();
		if (!host) return raw;
		const { lead, rest } = hostLabelParts(host, domain);
		const tail = `${parsed.pathname}${parsed.search}${parsed.hash}`;
		return [lead, rest, tail && tail !== '/' ? `…${tail.split('/').at(-1) || ''}` : null]
			.filter(Boolean).join(' · ');
	} catch {
		return raw;
	}
}

/**
 * The two parts a host reads as: the registrable domain leads, the subdomain in front of it follows.
 * One copy, shared by the pane's address and the chat chip, so the two never word a host differently.
 *
 * The backend's domain wins whenever the host sits inside it. With none supplied — a chip stored before
 * the field existed, a hand-typed address — the host's last two labels lead instead of riding past the
 * field's right-hand ellipsis: a rough cue, but on a lookalike like `accounts.example.com.other-site.net`
 * the end is the part that names the site. Nothing is dropped, only split, and an IP, a single label or a
 * host whose last label cannot be a TLD (no PSL here to tell) stands whole as itself.
 */
export function hostLabelParts(host: string, domain?: string | null): { lead: string; rest: string | null } {
	const matched = domain && (host === domain || host.endsWith(`.${domain}`)) ? domain : null;
	if (matched) return { lead: matched, rest: host.slice(0, host.length - matched.length).replace(/\.$/, '') || null };
	const labels = host.split('.');
	if (labels.length >= 3 && /^[a-z][a-z0-9-]*$/.test(labels[labels.length - 1])) {
		return { lead: labels.slice(-2).join('.'), rest: labels.slice(0, -2).join('.') };
	}
	return { lead: host, rest: null };
}

export function browserTabLabel(title: string | null | undefined, url: string, domain?: string | null): string {
	return title?.trim() || browserAddressLabel(url, domain) || 'Browser';
}
