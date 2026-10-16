import { inkOn, parseColor, stepToContrast, toHex } from './contrast';

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
 * The darkest surface accent text sits on in each theme (`--bg-tertiary`, `--bg-hover`), so
 * text that passes there passes on every lighter pane.
 */
const ACCENT_TEXT_SURFACES: Record<'light' | 'dark', string[]> = {
	light: ['#ffffff', '#f0f0f2', '#e8e8ec'],
	dark: ['#0a0a0b', '#1c1c1f', '#242428'],
};
const ON_FILL_LIGHT = '#ffffff';

/**
 * What a project's colour becomes when it is the accent: the fill stays the colour itself,
 * text in it is the same hue stepped until it reads (4.5:1) on every pane, and ink on the
 * fill takes the maximum-contrast ink from the shared `inkOn` rule. Primary-action labels
 * are ordinary small text, so their on-fill contrast stays readable.
 */
export function accentRoles(colour: string, theme: 'light' | 'dark'): { text: string; onFill: string } {
	const own = parseColor(colour);
	if (!own) return { text: colour, onFill: ON_FILL_LIGHT };
	const surfaces = ACCENT_TEXT_SURFACES[theme].map((hex) => parseColor(hex)!);
	const text = toHex(stepToContrast(own, surfaces, theme, 4.5));
	return { text, onFill: inkOn(colour) };
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
