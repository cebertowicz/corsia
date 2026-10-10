import { AGENT_COLORS } from '$lib/docPresence';

/** Document backgrounds from `app-tokens.css` (`--bg-primary`), and the focus tint an agent colour casts over them. */
const BACKGROUND = { light: '#ffffff', dark: '#0a0a0b' } as const;
const TINT = { light: 0.08, dark: 0.12 } as const;
const MIN_CONTRAST = 4.5;

export type Theme = keyof typeof BACKGROUND;
type Rgb = [number, number, number];

export interface SuggestionTones { light: string; dark: string }

const HEX = /^#([0-9a-f]{6})$/i;

function parse(hex: string): Rgb | null {
	const match = HEX.exec(hex.trim());
	if (!match) return null;
	const value = parseInt(match[1], 16);
	return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
}

const toHex = (rgb: Rgb) => `#${rgb.map((channel) => Math.round(channel).toString(16).padStart(2, '0')).join('')}`;

function luminance(rgb: Rgb): number {
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

const over = (colour: Rgb, background: Rgb, amount: number): Rgb =>
	colour.map((channel, i) => channel * amount + background[i] * (1 - amount)) as Rgb;

function toHsl([r, g, b]: Rgb): [number, number, number] {
	const [pr, pg, pb] = [r / 255, g / 255, b / 255];
	const max = Math.max(pr, pg, pb), min = Math.min(pr, pg, pb);
	const l = (max + min) / 2;
	if (max === min) return [0, 0, l];
	const d = max - min;
	const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
	const h = max === pr ? (pg - pb) / d + (pg < pb ? 6 : 0) : max === pg ? (pb - pr) / d + 2 : (pr - pg) / d + 4;
	return [h * 60, s, l];
}

function fromHsl([h, s, l]: [number, number, number]): Rgb {
	const chroma = (1 - Math.abs(2 * l - 1)) * s;
	const x = chroma * (1 - Math.abs(((h / 60) % 2) - 1));
	const m = l - chroma / 2;
	const [r, g, b] = h < 60 ? [chroma, x, 0] : h < 120 ? [x, chroma, 0] : h < 180 ? [0, chroma, x]
		: h < 240 ? [0, x, chroma] : h < 300 ? [x, 0, chroma] : [chroma, 0, x];
	return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/** What a tone can sit on: the document, and the focus tint of its own colour and of every other palette colour over it. */
function surfaces(own: Rgb, theme: Theme): Rgb[] {
	const background = parse(BACKGROUND[theme])!;
	const tinting = [own, ...AGENT_COLORS.map(parse).filter((rgb): rgb is Rgb => rgb !== null)];
	return [background, ...tinting.map((tint) => over(tint, background, TINT[theme]))];
}

const readable = (tone: Rgb, on: Rgb[]) => on.every((surface) => contrastRatio(tone, surface) >= MIN_CONTRAST);

/**
 * The agent's colour as text: itself where it already meets 4.5:1 on the document and on every focus tint,
 * otherwise the same hue stepped darker (light theme) or lighter (dark theme) until it does.
 * A colour that is not a six-digit hex is returned unchanged.
 */
export function readableTone(colour: string, theme: Theme): string {
	const own = parse(colour);
	if (!own) return colour;
	const on = surfaces(own, theme);
	if (readable(own, on)) return colour.toLowerCase();
	const [h, s, l] = toHsl(own);
	const step = theme === 'light' ? -0.005 : 0.005;
	for (let next = l + step; next >= 0 && next <= 1; next += step) {
		const tone = fromHsl([h, s, next]).map(Math.round) as Rgb;
		if (readable(tone, on)) return toHex(tone);
	}
	return theme === 'light' ? '#000000' : '#ffffff';
}

export const suggestionTones = (colour: string): SuggestionTones => ({ light: readableTone(colour, 'light'), dark: readableTone(colour, 'dark') });
