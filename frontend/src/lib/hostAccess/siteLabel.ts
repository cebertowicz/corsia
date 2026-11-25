import { width } from '$lib/accountAddress';

/**
 * The ways a saved login's site can read on one line in the chat entry, longest first. The scheme is never
 * shown (a fill is https only; the drawer shows the full address). A shorter form drops whole subdomain labels
 * from the front and keeps the end, so the cut never falls inside a label and the registrable domain (with any
 * port) always stays whole. "…" marks the cut, with no space after it.
 *
 * The backend names the registrable domain from the Public Suffix List. Without one (for an IP address it is
 * the address itself), the whole host is the tail, as the browser pane's address does.
 */
export function siteLabels(site: string, domain: string | null | undefined): string[] {
	const match = /^([a-z][a-z0-9+.-]*:\/\/)?([^/:]+)(:\d+)?(.*)$/i.exec(site);
	if (!match) return [site];
	const [, , host, port = '', rest] = match;
	const inside = !!domain && (host === domain || host.endsWith(`.${domain}`));
	const tailHost = inside ? domain! : host;
	const labels = inside && host !== domain ? host.slice(0, host.length - domain!.length - 1).split('.') : [];
	const tail = `${tailHost}${port}${rest}`;
	const out = [`${labels.map((label) => `${label}.`).join('')}${tail}`];
	// Drop one more label from the front each time, down to the domain alone.
	for (let kept = labels.length - 1; kept >= 0; kept--) {
		out.push(`…${labels.slice(labels.length - kept).map((label) => `${label}.`).join('')}${tail}`);
	}
	return out;
}

/**
 * Show the longest of `labels` that fits the element's width on one line; the last one if none does.
 * The element's own text is set here, so it renders empty, and it is refitted whenever its width changes.
 * It also sets `--label-full` to the longest label's width, so a layout can give the whole label room first,
 * and `--label-min` to the shortest label's width, so a layout never squeezes it below the domain.
 */
export function fitLabel(node: HTMLElement, labels: string[]) {
	let current = labels;
	let frame = 0;
	const fit = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			const full = `${Math.ceil(width(current[0] ?? '', getComputedStyle(node)))}px`;
			if (node.style.getPropertyValue('--label-full') !== full) node.style.setProperty('--label-full', full);
			const min = `${Math.ceil(width(current.at(-1) ?? '', getComputedStyle(node)))}px`;
			if (node.style.getPropertyValue('--label-min') !== min) node.style.setProperty('--label-min', min);
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
