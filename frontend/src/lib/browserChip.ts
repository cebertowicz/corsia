/**
 * The chip that reopens the shared browser from the Step whose tool call opened it in front of the person.
 *
 * The server decides whether a call earns one (a visible `browser_open` that succeeded) and stores `browser_chip` on
 * that call's Step as it settles; a Focus run stub carries the newest of its Steps'. This module only reads and words it.
 */
import { hostLabelParts } from './browserAddress';

export interface BrowserChip {
	/** The page's host without `www.`; `null` when the opened URL named none. */
	host: string | null;
	/** The page's registrable domain as the backend names it; `null` on chips stored before the field existed. */
	domain?: string | null;
}

/** The chip's tooltip: it reopens the one browser view, it does not navigate. */
export const BROWSER_CHIP_NAME = 'Reopen the shared browser';

/**
 * What the chip says: the registrable domain first, then the subdomain in front of it — the same lead as the pane's
 * address, so the part that names the site stays whole under the chip's own ellipsis. `Shared browser` when there is no host.
 */
export function browserChipLabel(chip: BrowserChip): string {
	if (!chip.host) return 'Shared browser';
	const { lead, rest } = hostLabelParts(chip.host, chip.domain);
	return rest ? `${lead} · ${rest}` : lead;
}

/**
 * The chip's accessible name: what it says, then what a click does, as a file chip names itself then its type. Starting
 * with the visible host lets voice control say it and tells one chip from another.
 */
export function browserChipAccessibleName(chip: BrowserChip): string {
	return chip.host ? `${browserChipLabel(chip)}, reopen the shared browser` : BROWSER_CHIP_NAME;
}

/** A `browser_chip` field as it arrives from the server; chips stored before `domain` existed parse with it `null`. */
export function parseBrowserChip(raw: unknown): BrowserChip | null {
	if (!raw || typeof raw !== 'object') return null;
	const host = (raw as { host?: unknown }).host;
	if (host !== null && typeof host !== 'string') return null;
	const domain = (raw as { domain?: unknown }).domain;
	if (domain !== undefined && domain !== null && typeof domain !== 'string') return null;
	return { host: host || null, domain: typeof domain === 'string' ? domain : null };
}
