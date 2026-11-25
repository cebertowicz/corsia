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

/** Sent from the site's element when its shortest label's width (`--label-min`) changes. */
const LABEL_FIT = 'labelfit';

/** The agent's name keeps this many em on the card's row while the site still fits there, at its domain if need be. */
const OWNER_READABLE_EM = 8;
/** Below this many em for the name, the site takes its own line instead. */
const OWNER_LEAST_EM = 4;

/**
 * Where a saved login's agent name and site go in the card, in CSS px. `room` is the row's content width,
 * `fixed` the width of everything shown on it but the name and the site, `items` how many things are shown,
 * name and site included, and `siteMin` the width of the site's shortest label (its registrable domain).
 * On one row the name keeps 8em while the site still fits beside it at its domain; with less room that floor
 * gives way, down to 4em; with less still, the site goes on its own line under the name, which keeps the row.
 */
export function rowLayout({ room, fixed, gap, items, siteMin, em }: {
	room: number; fixed: number; gap: number; items: number; siteMin: number; em: number;
}): { stacked: boolean; ownerFloor: number } {
	const oneRow = room - fixed - gap * (items - 1) - siteMin;
	if (oneRow >= OWNER_LEAST_EM * em) return { stacked: false, ownerFloor: Math.min(OWNER_READABLE_EM * em, oneRow) };
	const ownRow = room - fixed - gap * (items - 2);
	return { stacked: true, ownerFloor: Math.max(0, Math.min(OWNER_READABLE_EM * em, ownRow)) };
}

/**
 * Lay out a saved login's card row (see `rowLayout`): sets `--owner-floor` on the row and marks it
 * `data-stacked` when the site needs its own line. It refits when the row's width or contents change, or when
 * the site's shortest label is measured. Off (`enabled` false), it clears both.
 */
export function fitRow(node: HTMLElement, enabled: boolean) {
	let on = enabled;
	let frame = 0;
	const fit = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			const owner = node.querySelector<HTMLElement>(':scope > .pending-owner');
			const site = node.querySelector<HTMLElement>(':scope > .pending-site');
			if (!on || !owner || !site) {
				node.removeAttribute('data-stacked');
				node.style.removeProperty('--owner-floor');
				return;
			}
			const siteMin = Number.parseFloat(site.style.getPropertyValue('--label-min'));
			const style = getComputedStyle(node);
			const room = node.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
			// Not laid out yet, or the site not measured yet: its 'labelfit' brings this back.
			if (!Number.isFinite(siteMin) || !(room > 0)) return;
			const shown = [...node.children].filter((child) => getComputedStyle(child).display !== 'none');
			const fixed = shown
				.filter((child) => child !== owner && child !== site)
				.reduce((sum, child) => sum + child.getBoundingClientRect().width, 0);
			const { stacked, ownerFloor } = rowLayout({
				room, fixed, gap: Number.parseFloat(style.columnGap) || 0, items: shown.length, siteMin,
				em: Number.parseFloat(getComputedStyle(owner).fontSize),
			});
			node.toggleAttribute('data-stacked', stacked);
			const floor = `${Math.floor(ownerFloor)}px`;
			if (node.style.getPropertyValue('--owner-floor') !== floor) node.style.setProperty('--owner-floor', floor);
		});
	};
	const resized = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(fit);
	resized?.observe(node);
	// A badge shown or hidden (out of date, or checked again) changes what else is on the row.
	const changed = typeof MutationObserver === 'undefined' ? null : new MutationObserver(fit);
	changed?.observe(node, { childList: true });
	node.addEventListener(LABEL_FIT, fit);
	fit();
	return {
		update(next: boolean) { on = next; fit(); },
		destroy() {
			cancelAnimationFrame(frame);
			resized?.disconnect();
			changed?.disconnect();
			node.removeEventListener(LABEL_FIT, fit);
		},
	};
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
			if (node.style.getPropertyValue('--label-min') !== min) {
				node.style.setProperty('--label-min', min);
				node.dispatchEvent(new Event(LABEL_FIT, { bubbles: true }));
			}
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
