/**
 * The chip that reopens the shared browser from the message whose tool call opened it in front of the person.
 *
 * The server decides whether a call earns one (a settled, visible `browser_open`) and puts `browser_chip` on that
 * call, on a Focus run stub, and on live frames alike. This module only reads it: it picks the last one and words it.
 */
export interface BrowserChip {
	/** The page's host without `www.`; `null` when the opened URL named none. */
	host: string | null;
}

/** The chip's accessible name and tooltip: it reopens the one browser view, it does not navigate. */
export const BROWSER_CHIP_NAME = 'Reopen the shared browser';

/** What the chip says: the host the agent opened, or `Shared browser` when there is none. */
export function browserChipLabel(chip: BrowserChip): string {
	return chip.host ?? 'Shared browser';
}

/** A `browser_chip` field as it arrives from the server. */
export function parseBrowserChip(raw: unknown): BrowserChip | null {
	if (!raw || typeof raw !== 'object') return null;
	const host = (raw as { host?: unknown }).host;
	if (host !== null && typeof host !== 'string') return null;
	return { host: host || null };
}

/** Every open in a thread reopens the same browser view, so a message gets one chip, for its last call that has one. */
export function lastBrowserChip(calls: unknown): BrowserChip | null {
	if (!Array.isArray(calls)) return null;
	for (let i = calls.length - 1; i >= 0; i--) {
		const chip = parseBrowserChip(calls[i]?.browser_chip);
		if (chip) return chip;
	}
	return null;
}
