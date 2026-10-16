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
 * The same hue stepped darker (light theme) or lighter (dark theme) until it meets `min`
 * against every surface; the colour itself when it already does.
 */
export function stepToContrast(own: Rgb, on: Rgb[], theme: 'light' | 'dark', min: number): Rgb {
	const meets = (tone: Rgb) => on.every((surface) => contrastRatio(tone, surface) >= min);
	if (meets(own)) return own;
	const [h, s, l] = toHsl(own);
	const step = theme === 'light' ? -0.005 : 0.005;
	for (let next = l + step; next >= 0 && next <= 1; next += step) {
		const tone = fromHsl([h, s, next]).map(Math.round) as Rgb;
		if (meets(tone)) return tone;
	}
	return theme === 'light' ? [0, 0, 0] : [255, 255, 255];
}

/** White or near-black, whichever reads better on `fill`; white when the fill is not a colour this parses. */
export function inkOn(fill: string): string {
	const rgb = parseColor(fill);
	if (!rgb) return '#ffffff';
	return contrastRatio(rgb, [255, 255, 255]) >= contrastRatio(rgb, [10, 10, 11]) ? '#ffffff' : '#0a0a0b';
}
