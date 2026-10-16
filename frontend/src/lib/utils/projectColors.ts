import { inkOn, parseColor, parseCssColour, stepToContrast, toHex } from './contrast';

const PALETTE_HUES = [
	210,
	155,
	38,
	280,
	340,
	175,
	60,
	310,
	20,
	245,
];

function paletteIndex(id: string): number {
	let sum = 0;
	for (let i = 0; i < id.length; i++) {
		sum += id.charCodeAt(i) * (i + 1);
	}
	return sum % PALETTE_HUES.length;
}

export function projectColor(id: string | null | undefined): string {
	if (!id) return 'hsl(260, 70%, 62%)';
	const hue = PALETTE_HUES[paletteIndex(id)];
	return `hsl(${hue}, 70%, 62%)`;
}

export function projectAccent(id: string | null | undefined, accentColor?: string | null): string {
	return accentColor || projectColor(id);
}

/**
 * The surfaces accent text sits on in each theme (`--bg-primary`, `--bg-tertiary`, `--bg-hover`), copied from
 * `app-tokens.css` and pinned to it by `projectColors.test.ts`. The limiting one is the darkest in light (stepped text
 * darkens) and the lightest in dark (stepped text lightens), so text that passes all of them passes on every pane.
 */
export const ACCENT_TEXT_SURFACES: Record<'light' | 'dark', string[]> = {
	light: ['#ffffff', '#f0f0f2', '#e8e8ec'],
	dark: ['#0a0a0b', '#1c1c1f', '#242428'],
};
const ON_FILL_LIGHT = '#ffffff';

export interface AccentRoles {
	base: string;
	text: string;
	wash: string;
	line: string;
	fill: string;
	onFill: string;
}

/** The token each role paints through, defined with its default in `app-tokens.css`. */
export const ACCENT_ROLE_TOKENS: Record<keyof AccentRoles, string> = {
	base: '--accent',
	text: '--accent-text',
	wash: '--accent-wash',
	line: '--accent-line',
	fill: '--accent-fill',
	onFill: '--accent-on-fill',
};

const warnedUnparsed = new Set<string>();

/**
 * What a project's colour becomes when it is the accent, every role from this one place.
 * The colour is treated as opaque: an alpha component is dropped, and every role is opaque (the base is the colour
 * itself, or its opaque hex when it carried an alpha). Text is the same hue stepped until it reads (4.5:1) on every
 * pane. The fill is the colour itself, and the ink on it is the shared `inkOn` choice; near the crossover where neither
 * ink reaches 4.5:1, the fill steps away from its ink until it does, so the ink is always
 * chosen for the exact colour behind it.
 * Null when the colour does not parse (the API validates it, so this should not happen): the default accent applies,
 * with one console warning per value.
 */
export function accentRoles(colour: string, theme: 'light' | 'dark'): AccentRoles | null {
	const parsed = parseCssColour(colour);
	if (!parsed) {
		if (!warnedUnparsed.has(colour)) {
			warnedUnparsed.add(colour);
			console.warn(`Project accent colour ${JSON.stringify(colour)} is not a colour; using the default accent.`);
		}
		return null;
	}
	const own = parsed.rgb;
	const base = parsed.alpha < 1 ? toHex(own) : colour;
	const wash = `color-mix(in srgb, ${base} 12%, transparent)`;
	const line = `color-mix(in srgb, ${base} 30%, transparent)`;
	const surfaces = ACCENT_TEXT_SURFACES[theme].map((hex) => parseColor(hex)!);
	const text = toHex(stepToContrast(own, surfaces, theme, 4.5));
	const onFill = inkOn(base);
	// White ink needs a darker fill and near-black ink a lighter one: the same stepping
	// direction as text in the light and dark themes respectively.
	const fill = toHex(stepToContrast(own, [parseColor(onFill)!], onFill === ON_FILL_LIGHT ? 'light' : 'dark', 4.5));
	return { base, text, wash, line, fill, onFill };
}

export const ACCENT_SWATCHES = [
	'#0284c7',
	'#2563eb',
	'#4f46e5',
	'#7c3aed',
	'#9333ea',
	'#c026d3',
	'#db2777',
	'#e11d48',
	'#dc2626',
	'#ea580c',
	'#d97706',
	'#ca8a04',
	'#16a34a',
	'#059669',
	'#0d9488',
	'#0891b2',
];

export const ACCENT_AUTO_GRADIENT = `conic-gradient(from 0deg, ${[...ACCENT_SWATCHES, ACCENT_SWATCHES[0]].join(', ')})`;
