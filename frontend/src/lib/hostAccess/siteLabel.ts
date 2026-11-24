import { width } from '$lib/accountAddress';

/**
 * The ways a saved login's site can read on one line, longest first. A shorter one drops whole segments from
 * the middle: the scheme ("https://") and each subdomain label are segments, the cut falls only between them,
 * and the registrable domain (with any port) always stays whole at the end. "…" marks the cut, with no spaces.
 *
 * The backend names the registrable domain from the Public Suffix List. Without one (an IP address, or a host
 * it couldn't name), the whole host is the tail, as the browser pane's address does.
 */
export function siteLabels(site: string, domain: string | null | undefined): string[] {
	const match = /^([a-z][a-z0-9+.-]*:\/\/)?([^/:]+)(:\d+)?(.*)$/i.exec(site);
	if (!match) return [site];
	const [, scheme = '', host, port = '', rest] = match;
	const inside = !!domain && (host === domain || host.endsWith(`.${domain}`));
	const tailHost = inside ? domain! : host;
	const labels = inside && host !== domain ? host.slice(0, host.length - domain!.length - 1).split('.') : [];
	const segments = [...(scheme ? [scheme] : []), ...labels.map((label) => `${label}.`)];
	const tail = `${tailHost}${port}${rest}`;
	const out = [`${segments.join('')}${tail}`];
	const seen = new Set(out);
	const cut: Array<{ text: string; head: number }> = [];
	// Keep `head` segments from the front and `back` from the end; at least one segment goes.
	for (let head = segments.length; head >= 0; head--) {
		for (let back = segments.length - head - 1; back >= 0; back--) {
			const front = segments.slice(0, head).join('').replace(/\.$/, '');
			const end = segments.slice(segments.length - back).join('');
			cut.push({ text: `${front}…${end}${tail}`, head });
		}
	}
	// Longest first; between two of a length, the one keeping more of the front.
	cut.sort((a, b) => b.text.length - a.text.length || b.head - a.head);
	for (const { text } of cut) {
		if (!seen.has(text)) {
			seen.add(text);
			out.push(text);
		}
	}
	return out;
}

/**
 * Show the longest of `labels` that fits the element's width on one line; the last one if none does.
 * The element's own text is set here, so it renders empty, and it is refitted whenever its width changes.
 */
export function fitLabel(node: HTMLElement, labels: string[]) {
	let current = labels;
	let frame = 0;
	const fit = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			const room = node.clientWidth;
			// Not laid out (hidden, or not shown yet): keep the whole site until there is room to measure.
			if (!room) {
				node.textContent = current[0] ?? '';
				return;
			}
			const style = getComputedStyle(node);
			node.textContent = current.find((label) => width(label, style) <= room) ?? current.at(-1) ?? '';
			// Canvas and layout can round differently: step down until the line really fits.
			let index = current.indexOf(node.textContent);
			while (node.scrollWidth > node.clientWidth + 0.5 && index < current.length - 1) {
				index += 1;
				node.textContent = current[index];
			}
		});
	};
	node.textContent = current[0] ?? '';
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
	observer?.observe(node);
	fit();
	return {
		update(next: string[]) { current = next; fit(); },
		destroy() { cancelAnimationFrame(frame); observer?.disconnect(); },
	};
}
