import { AGENT_COLORS } from '$lib/docPresence';
import { type Rgb, contrastRatio, over, parseHex as parse, stepToContrast, toHex } from '$lib/utils/contrast';

export { contrastRatio };

/** Document backgrounds from `app-tokens.css` (`--bg-primary`), and the focus tint an agent colour casts over them. */
const BACKGROUND = { light: '#ffffff', dark: '#0a0a0b' } as const;
const TINT = { light: 0.08, dark: 0.12 } as const;
const MIN_CONTRAST = 4.5;

export type Theme = keyof typeof BACKGROUND;

export interface SuggestionTones { light: string; dark: string }

/** What a tone can sit on: the document, and the focus tint of its own colour and of every other palette colour over it. */
function surfaces(own: Rgb, theme: Theme): Rgb[] {
	const background = parse(BACKGROUND[theme])!;
	const tinting = [own, ...AGENT_COLORS.map(parse).filter((rgb): rgb is Rgb => rgb !== null)];
	return [background, ...tinting.map((tint) => over(tint, background, TINT[theme]))];
}

/**
 * The agent's colour as text: itself where it already meets 4.5:1 on the document and on every focus tint,
 * otherwise the same hue stepped darker (light theme) or lighter (dark theme) until it does.
 * A colour that is not a six-digit hex is returned unchanged.
 */
export function readableTone(colour: string, theme: Theme): string {
	const own = parse(colour);
	if (!own) return colour;
	const tone = stepToContrast(own, surfaces(own, theme), theme, MIN_CONTRAST);
	return tone === own ? colour.toLowerCase() : toHex(tone);
}

export const suggestionTones = (colour: string): SuggestionTones => ({ light: readableTone(colour, 'light'), dark: readableTone(colour, 'dark') });
