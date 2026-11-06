import { Lexer, type Token } from 'marked';
import { resolveImagePath } from './markdown-image';

/** Inline images the document's image comment parser supports. */
const IMAGE = /^!\[(?:[^\[\]\\\n]|\\.)*\]\(\s*(<[^<>\n]*>|(?:[^\s()\\]|\\.|\([^\s()]*\))*)(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?\s*\)$/;

type Slot = { from: number; to: number } | null;

/** One slot per rendered local image, including HTML images which cannot carry image comments. */
export function cellImageSlots(source: string, docPath: string | null): Slot[] {
	const slots: Slot[] = [];
	const visit = (tokens: Token[], base: number, raw: string) => {
		let cursor = 0;
		for (const token of tokens) {
			const at = raw.indexOf(token.raw, cursor);
			if (at < 0) return false;
			cursor = at + token.raw.length;
			const from = base + at;
			if (token.type === 'image') {
				if (resolveImagePath(docPath, token.href) !== null) slots.push(IMAGE.test(token.raw) ? { from, to: from + token.raw.length } : null);
			} else if (token.type === 'html') {
				const template = document.createElement('template');
				template.innerHTML = token.raw;
				for (const img of template.content.querySelectorAll('img')) {
					if (resolveImagePath(docPath, img.getAttribute('src') ?? '') !== null) slots.push(null);
				}
			} else if ('tokens' in token && Array.isArray(token.tokens)) {
				if (!visit(token.tokens, from, token.raw)) return false;
			}
		}
		return true;
	};
	return visit(Lexer.lexInline(source, { gfm: true, breaks: true }), 0, source) ? slots : [];
}
