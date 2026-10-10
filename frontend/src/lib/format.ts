const TOKEN_UNITS = [{ at: 1_000_000_000, suffix: 'B' }, { at: 1_000_000, suffix: 'M' }, { at: 1000, suffix: 'k' }];

/**
 * A token count in short form: 850, 1.2k, 12k, 1.5M, 7.4B. With significant digits it keeps that many, trailing zeros
 * trimmed, so 4 reads 7.44B, 34.59B and 1.5M.
 */
export function humanTokens(n: number, significantDigits?: number): string {
	if (significantDigits !== undefined) return significantTokens(n, significantDigits);
	if (n >= 1_000_000_000) {
		const b = n / 1_000_000_000;
		return `${Number.isInteger(b) ? b : b.toFixed(1)}B`;
	}
	if (n >= 1_000_000) {
		const m = n / 1_000_000;
		return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
	}
	if (n >= 10_000) return `${Math.round(n / 1000)}k`;
	if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
	return `${n}`;
}

function significantTokens(n: number, digits: number): string {
	let unit = TOKEN_UNITS.findIndex((u) => n >= u.at);
	if (unit < 0) return `${n}`;
	let value = Number((n / TOKEN_UNITS[unit].at).toPrecision(digits));
	// Rounding can carry into the next unit: 999,999 is 1M, never 1000k.
	if (value >= 1000 && unit > 0) { unit -= 1; value = Number((n / TOKEN_UNITS[unit].at).toPrecision(digits)); }
	return `${value}${TOKEN_UNITS[unit].suffix}`;
}
