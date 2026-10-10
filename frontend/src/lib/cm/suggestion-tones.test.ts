import { describe, expect, it } from 'vitest';
import { AGENT_COLORS } from '$lib/docPresence';
import { contrastRatio, readableTone, suggestionTones } from './suggestion-tones';

const rgb = (hex: string): [number, number, number] => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as [number, number, number];
const mix = (colour: string, background: string, amount: number) =>
	rgb(colour).map((channel, i) => channel * amount + rgb(background)[i] * (1 - amount)) as [number, number, number];
const BACKGROUND = { light: '#ffffff', dark: '#0a0a0b' };
const TINT = { light: 0.08, dark: 0.12 };

describe('suggestion tones', () => {
	it.each(['light', 'dark'] as const)('every palette colour reads at 4.5:1 on the %s document and on every agent tint', (theme) => {
		for (const colour of AGENT_COLORS) {
			const tone = readableTone(colour, theme);
			for (const surface of [rgb(BACKGROUND[theme]), ...AGENT_COLORS.map((other) => mix(other, BACKGROUND[theme], TINT[theme]))]) {
				expect(contrastRatio(rgb(tone), surface), `${colour} ${theme} ${tone}`).toBeGreaterThanOrEqual(4.5);
			}
		}
	});
	it('darkens a colour that is too light for the light theme and keeps its hue family', () => {
		const tone = readableTone('#60a5fa', 'light');
		expect(tone).not.toBe('#60a5fa');
		const [r, g, b] = rgb(tone);
		expect(b).toBeGreaterThan(r);
		expect(contrastRatio(rgb(tone), rgb('#ffffff'))).toBeGreaterThanOrEqual(4.5);
	});
	it('lightens a colour that is too dark for the dark theme', () => {
		const tone = readableTone('#8b5cf6', 'dark');
		expect(contrastRatio(rgb(tone), rgb('#0a0a0b'))).toBeGreaterThanOrEqual(4.5);
	});
	it('leaves an already readable colour and a non-hex colour alone', () => {
		expect(readableTone('#60a5fa', 'dark')).toBe('#60a5fa');
		expect(readableTone('var(--text-tertiary)', 'light')).toBe('var(--text-tertiary)');
		expect(suggestionTones('rebeccapurple')).toEqual({ light: 'rebeccapurple', dark: 'rebeccapurple' });
	});
});
