const MULTIPART_PUBLIC_SUFFIXES = new Set(['co.uk', 'org.uk', 'ac.uk', 'com.au', 'net.au', 'org.au', 'co.jp']);

/** The visible address leads with the registrable domain and only elides the middle. */
export function registrableDomain(hostname: string): string {
	const labels = hostname.toLowerCase().split('.').filter(Boolean);
	if (labels.length <= 2) return labels.join('.');
	const suffix = labels.slice(-2).join('.');
	return MULTIPART_PUBLIC_SUFFIXES.has(suffix) && labels.length >= 3
		? labels.slice(-3).join('.')
		: labels.slice(-2).join('.');
}

export function browserAddressLabel(raw: string): string {
	try {
		const parsed = new URL(raw);
		const domain = registrableDomain(parsed.hostname);
		const subdomain = parsed.hostname.slice(0, Math.max(0, parsed.hostname.length - domain.length)).replace(/\.$/, '');
		const tail = `${parsed.pathname}${parsed.search}${parsed.hash}`;
		return [domain, subdomain || null, tail && tail !== '/' ? `…${tail.split('/').at(-1) || ''}` : null]
			.filter(Boolean).join(' · ');
	} catch {
		return raw;
	}
}

export function browserTabLabel(title: string | null | undefined, url: string): string {
	return title?.trim() || browserAddressLabel(url) || 'Browser';
}
