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
		const lead = domain && (host === domain || host.endsWith(`.${domain}`)) ? domain : host;
		const subdomain = host.slice(0, Math.max(0, host.length - lead.length)).replace(/\.$/, '');
		const tail = `${parsed.pathname}${parsed.search}${parsed.hash}`;
		return [lead, subdomain || null, tail && tail !== '/' ? `…${tail.split('/').at(-1) || ''}` : null]
			.filter(Boolean).join(' · ');
	} catch {
		return raw;
	}
}

export function browserTabLabel(title: string | null | undefined, url: string, domain?: string | null): string {
	return title?.trim() || browserAddressLabel(url, domain) || 'Browser';
}
