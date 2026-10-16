/** sRGB colour math for picking readable tones (WCAG 2 relative luminance and contrast). */

export type Rgb = [number, number, number];

const HEX = /^#([0-9a-f]{6})$/i;
const ANY_HEX = /^#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const FUNCTION = /^(rgba?|hsla?)\(\s*(.*?)\s*\)$/i;
const ARGUMENT = /^(\d+(?:\.\d+)?|\.\d+)(%?)$/;

export function parseHex(hex: string): Rgb | null {
	const match = HEX.exec(hex.trim());
	if (!match) return null;
	const value = parseInt(match[1], 16);
	return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

const clamp = (value: number, max: number) => Math.min(Math.max(value, 0), max);

/**
 * Every colour form the project API accepts (`_ACCENT_COLOR_RE` in `backend/api/project_types.py`): `#rgb`, `#rgba`,
 * `#rrggbb`, `#rrggbbaa`, and `rgb()`, `rgba()`, `hsl()`, `hsla()` in comma or space syntax, with an optional
 * alpha (`/ a` in space syntax). Null for anything else, including text that matches that pattern but is not a colour.
 */
export function parseCssColour(colour: string): { rgb: Rgb; alpha: number } | null {
	const text = colour.trim();
	const hex = ANY_HEX.exec(text);
	if (hex) {
		const digits = hex[1].length <= 4 ? [...hex[1]].map((digit) => digit + digit) : hex[1].match(/../g)!;
		const [r, g, b, a = 255] = digits.map((pair) => parseInt(pair, 16));
		return { rgb: [r, g, b], alpha: a / 255 };
	}
	const call = FUNCTION.exec(text);
	if (!call) return null;
	const isHsl = call[1].toLowerCase().startsWith('hsl');
	const commas = call[2].includes(',');
	let parts: string[];
	let alphaText: string | undefined;
	if (commas) {
		parts = call[2].split(',').map((part) => part.trim());
		if (parts.length === 4) alphaText = parts.pop();
	} else {
		const [channels, alpha, extra] = call[2].split('/');
		if (extra !== undefined) return null;
		parts = channels.trim().split(/\s+/);
		alphaText = alpha?.trim();
	}
	if (parts.length !== 3) return null;
	const args = parts.map((part) => ARGUMENT.exec(part));
	if (!args.every((arg): arg is RegExpExecArray => arg !== null)) return null;
	const [first, second, third] = args;
	let alpha = 1;
	if (alphaText !== undefined) {
		const arg = ARGUMENT.exec(alphaText);
		if (!arg) return null;
		alpha = clamp(Number(arg[1]) / (arg[2] ? 100 : 1), 1);
	}
	if (isHsl) {
		// The hue is a bare number; saturation and lightness are percentages, or bare numbers in space syntax.
		if (first[2] || (commas && (!second[2] || !third[2]))) return null;
		const unit = (arg: RegExpExecArray) => clamp(Number(arg[1]), 100) / 100;
		return { rgb: fromHsl([Number(first[1]) % 360, unit(second), unit(third)]), alpha };
	}
	// Comma syntax takes all numbers or all percentages; space syntax may mix them.
	if (commas && new Set(args.map((arg) => arg[2])).size > 1) return null;
	const channel = (arg: RegExpExecArray) => clamp(Number(arg[1]) * (arg[2] ? 2.55 : 1), 255);
	return { rgb: [channel(first), channel(second), channel(third)], alpha };
}

/** Any colour `parseCssColour` reads, with its alpha dropped; anything else is null. */
export function parseColor(colour: string): Rgb | null {
	return parseCssColour(colour)?.rgb ?? null;
}

export const toHex = (rgb: Rgb) => `#${rgb.map((channel) => Math.round(channel).toString(16).padStart(2, '0')).join('')}`;

export function luminance(rgb: Rgb): number {
	const [r, g, b] = rgb.map((channel) => {
		const unit = channel / 255;
		return unit <= 0.03928 ? unit / 12.92 : ((unit + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: Rgb, b: Rgb): number {
	const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
	return (light + 0.05) / (dark + 0.05);
}

export const over = (colour: Rgb, background: Rgb, amount: number): Rgb =>
	colour.map((channel, i) => channel * amount + background[i] * (1 - amount)) as Rgb;

export function toHsl([r, g, b]: Rgb): [number, number, number] {
	const [pr, pg, pb] = [r / 255, g / 255, b / 255];
	const max = Math.max(pr, pg, pb), min = Math.min(pr, pg, pb);
	const l = (max + min) / 2;
	if (max === min) return [0, 0, l];
	const d = max - min;
	const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
	const h = max === pr ? (pg - pb) / d + (pg < pb ? 6 : 0) : max === pg ? (pb - pr) / d + 2 : (pr - pg) / d + 4;
	return [h * 60, s, l];
}

export function fromHsl([h, s, l]: [number, number, number]): Rgb {
	const chroma = (1 - Math.abs(2 * l - 1)) * s;
	const x = chroma * (1 - Math.abs(((h / 60) % 2) - 1));
	const m = l - chroma / 2;
	const [r, g, b] = h < 60 ? [chroma, x, 0] : h < 120 ? [x, chroma, 0] : h < 180 ? [0, chroma, x]
		: h < 240 ? [0, x, chroma] : h < 300 ? [x, 0, chroma] : [chroma, 0, x];
	return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/**
 * APCA's contrast model, SAPC-8 with the "0.0.98G-4g" constant group (the constants the W3 draft shipped).
 * Formula and constants: SAPC-APCA by Andrew Somers, https://github.com/Myndex/SAPC-APCA — reimplemented
 * here, not vendored. APCA is the WCAG 3 draft model, built to fix the one place WCAG 2 is wrong: a
 * mid-tone fill, where WCAG 2's ratio says black and white read the same and they plainly do not.
 * `norm*` are the exponents for dark ink on a lighter fill, `rev*` for light ink on a darker one.
 */
const APCA = {
	/** APCA emulates monitor perception with a plain 2.4 power instead of WCAG 2's piecewise curve. */
	monitorExponent: 2.4,
	srgbY: [0.2126729, 0.7151522, 0.072175] as const,
	normBG: 0.56,
	normTXT: 0.57,
	revTXT: 0.62,
	revBG: 0.65,
	/** A near-black Y below this is soft-clamped upwards, so black on black still reads as no contrast. */
	blkThrs: 0.022,
	blkClmp: 1.414,
	scale: 1.14,
	/** Raw SAPC values under this collapse to 0: they would flip polarity before they read as nothing. */
	loClip: 0.1,
	offset: 0.027,
	deltaYMin: 0.0005,
};

/** APCA's linear-light Y for an sRGB colour: each channel at 2.4, then the sRGB coefficients. */
export function apcaY([r, g, b]: Rgb): number {
	const channel = (value: number) => (value / 255) ** APCA.monitorExponent;
	return APCA.srgbY[0] * channel(r) + APCA.srgbY[1] * channel(g) + APCA.srgbY[2] * channel(b);
}

/**
 * APCA lightness contrast (`Lc`) of `text` on `background`. Signed as APCA signs it: negative for light ink
 * on a dark fill, positive for dark ink on a light fill — never take the sign as meaningful, only the
 * magnitude. Unlike `contrastRatio` this is polarity sensitive, and its scale is perceptual: 75 is body text,
 * 60 is the floor for large text, and white on black is about -106 rather than an unbounded 21.
 */
export function apcaLc(text: Rgb, background: Rgb): number {
	const softClamp = (y: number) => (y > APCA.blkThrs ? y : y + (APCA.blkThrs - y) ** APCA.blkClmp);
	const txtY = softClamp(apcaY(text));
	const bgY = softClamp(apcaY(background));
	if (Math.abs(bgY - txtY) < APCA.deltaYMin) return 0;
	if (bgY > txtY) {
		const sapc = (bgY ** APCA.normBG - txtY ** APCA.normTXT) * APCA.scale;
		return (sapc < APCA.loClip ? 0 : sapc - APCA.offset) * 100;
	}
	const sapc = (bgY ** APCA.revBG - txtY ** APCA.revTXT) * APCA.scale;
	return (sapc > -APCA.loClip ? 0 : sapc + APCA.offset) * 100;
}

/**
 * The same hue stepped darker (light theme) or lighter (dark theme) until it meets `min`
 * against every surface; the colour itself when it already does.
 */
export function stepToContrast(own: Rgb, on: Rgb[], theme: 'light' | 'dark', min: number): Rgb {
	return stepUntil(own, theme, (tone) => on.every((surface) => contrastRatio(tone, surface) >= min));
}

/** The same hue stepped darker (light) or lighter (dark) until `meets` holds; `own` itself (same object) when it does. */
export function stepUntil(own: Rgb, theme: 'light' | 'dark', meets: (tone: Rgb) => boolean): Rgb {
	if (meets(own)) return own;
	const [h, s, l] = toHsl(own);
	const step = theme === 'light' ? -0.005 : 0.005;
	for (let next = l + step; next >= 0 && next <= 1; next += step) {
		const tone = fromHsl([h, s, next]).map(Math.round) as Rgb;
		if (meets(tone)) return tone;
	}
	return theme === 'light' ? [0, 0, 0] : [255, 255, 255];
}

/** The two inks an accent fill may carry. */
export const INK_WHITE = '#ffffff';
const INK_BLACK = '#0a0a0b';

/**
 * White or near-black, whichever APCA reads better on `fill` — the larger `|Lc|` of the two. WCAG 2's ratio
 * cannot make this call on a mid-tone fill: it rates black and white on `#0083af` within half a point of each
 * other, and picks black, which reads badly on a mid blue. White when the fill is not a colour this parses.
 */
export function inkOn(fill: string): string {
	const rgb = parseColor(fill);
	if (!rgb) return INK_WHITE;
	const reads = (ink: string) => Math.abs(apcaLc(parseHex(ink)!, rgb));
	return reads(INK_WHITE) >= reads(INK_BLACK) ? INK_WHITE : INK_BLACK;
}
