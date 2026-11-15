/**
 * Open spans on the browser's clock. The server sends each open span as its start
 * time on the server's clock; a live count of `now - start` on the browser would carry
 * any skew between the two clocks (a phone or a second computer on remote access).
 * Every plan read moves its open starts onto the browser clock instead, by the offset
 * the latest operator read measured, so a count runs from arrival as `$lib/openTime` does.
 */
import { parseWsTimestamp } from '$lib/time';

/** Browser clock minus server clock (ms), from the latest operator read; 0 until one arrives. */
let offsetMs = 0;

/** Measure the offset from a read's server `as_of` and the browser time it arrived. */
export function measureServerClock(asOf: string, receivedAt: number): void {
	const offset = receivedAt - parseWsTimestamp(asOf);
	if (Number.isFinite(offset)) offsetMs = offset;
}

/** Shift every `open_starts` entry under `value` by the measured offset, in place. */
export function onClientClock(value: unknown, offset = offsetMs): void {
	if (offset === 0) return;
	if (Array.isArray(value)) {
		value.forEach((item) => onClientClock(item, offset));
	} else if (value !== null && typeof value === 'object') {
		const record = value as Record<string, unknown>;
		if (Array.isArray(record.open_starts)) {
			record.open_starts = record.open_starts.map((start) => new Date(parseWsTimestamp(start) + offset).toISOString());
		}
		for (const [key, child] of Object.entries(record)) {
			if (key !== 'open_starts') onClientClock(child, offset);
		}
	}
}
