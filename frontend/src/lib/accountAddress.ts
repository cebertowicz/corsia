/** Pure helpers that fit a long account address into a closed Connected apps row title. */
export type AccountAddress = { localPrefix: string; localSuffix: string; domain: string };
let textContext: CanvasRenderingContext2D | null = null;

export function accountAddress(address: string): AccountAddress | null {
	const at = address.lastIndexOf('@');
	if (at <= 0 || at === address.length - 1) return null;
	const local = address.slice(0, at);
	const suffixStart = Math.max(local.lastIndexOf('.'), local.lastIndexOf('_'), local.lastIndexOf('-'), local.lastIndexOf('+'));
	return { localPrefix: suffixStart > 0 ? local.slice(0, suffixStart) : local, localSuffix: suffixStart > 0 ? local.slice(suffixStart) : '', domain: address.slice(at + 1) };
}
export function localSegments(prefix: string): string[] { return prefix.split(/[._+\-]+/).filter(Boolean); }
export function width(text: string, style: CSSStyleDeclaration): number {
	if (!textContext && typeof document !== 'undefined') textContext = document.createElement('canvas').getContext('2d');
	if (!textContext) return text.length * 8;
	textContext.font = style.font || `${style.fontSize} ${style.fontFamily}`;
	const letterSpacing = Number.parseFloat(style.letterSpacing) || 0;
	return textContext.measureText(text).width + Math.max(0, text.length - 1) * letterSpacing;
}
export function clippedPrefix(prefix: string, available: number, style: CSSStyleDeclaration): string {
	if (width(prefix, style) <= available) return prefix;
	const chars = [...prefix];
	let low = 0; let high = chars.length;
	while (low < high) {
		const middle = Math.ceil((low + high) / 2);
		if (width(`${chars.slice(0, middle).join('')}…`, style) <= available) low = middle;
		else high = middle - 1;
	}
	return `${chars.slice(0, low).join('').replace(/[._+\-]+$/, '')}…`;
}
