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
