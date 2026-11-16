/**
 * The visible address leads with the page's registrable domain and only elides the middle.
 *
 * The backend names that domain from the Public Suffix List (host_access/browser_domain.py), the same
 * one the password step's title uses, so the pane never guesses where one site ends and another starts.
 * Without it, as for a URL with no host, the whole host leads.
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
 * The backend's domain leads whenever the host sits inside it. Without one (a chip stored before the field
 * existed) the whole host stands as itself: only the Public Suffix List knows where a site's domain starts.
 */
export function hostLabelParts(host: string, domain?: string | null): { lead: string; rest: string | null } {
	if (!domain || !(host === domain || host.endsWith(`.${domain}`))) return { lead: host, rest: null };
	return { lead: domain, rest: host.slice(0, host.length - domain.length).replace(/\.$/, '') || null };
}

export function browserTabLabel(title: string | null | undefined, url: string, domain?: string | null): string {
	return title?.trim() || browserAddressLabel(url, domain) || 'Browser';
}
