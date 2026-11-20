import { Lexer, type Token } from 'marked';
import { resolveImagePath } from './markdown-image';

/** Inline images the document's image comment parser supports. */
const IMAGE = /^!\[(?:[^\[\]\\\n]|\\.)*\]\(\s*(<[^<>\n]*>|(?:[^\s()\\]|\\.|\([^\s()]*\))*)(?:\s+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?\s*\)$/;

type Slot = { from: number; to: number } | null;

/** The characters `html` draws, entities decoded, as the cell's rendered text holds them. */
const drawnText = (html: string) => {
	const template = document.createElement('template');
	template.innerHTML = html;
	return template.content.textContent ?? '';
};

/**
 * A cell's source as the table draws it (`renderedFragment`): one slot per local image, which is drawn as an image (null for an
 * HTML `<img>`, which cannot carry an image comment), and whether it draws any text. An image that is not local shows as its alt
 * text, or its address when it has none, so it is text.
 */
function readCell(source: string, docPath: string | null): { slots: Slot[]; text: boolean } {
	const slots: Slot[] = [];
	let text = false;
	const visit = (tokens: Token[], base: number, raw: string) => {
		let cursor = 0;
		for (const token of tokens) {
			const at = raw.indexOf(token.raw, cursor);
			if (at < 0) return false;
			cursor = at + token.raw.length;
			const from = base + at;
			if (token.type === 'image') {
				if (resolveImagePath(docPath, token.href) !== null) slots.push(IMAGE.test(token.raw) ? { from, to: from + token.raw.length } : null);
				else if ((token.text || token.href).trim()) text = true;
			} else if (token.type === 'html') {
				const template = document.createElement('template');
				template.innerHTML = token.raw;
				for (const img of template.content.querySelectorAll('img')) {
					const src = img.getAttribute('src') ?? '';
					if (resolveImagePath(docPath, src) !== null) slots.push(null);
					else if ((img.getAttribute('alt') || src).trim()) text = true;
				}
				if ((template.content.textContent ?? '').trim()) text = true;
			} else if ('tokens' in token && Array.isArray(token.tokens)) {
				if (!visit(token.tokens, from, token.raw)) return false;
			} else if ('text' in token && typeof token.text === 'string' && drawnText(token.text).trim()) {
				text = true;
			}
		}
		return true;
	};
	return visit(Lexer.lexInline(source, { gfm: true, breaks: true }), 0, source) ? { slots, text } : { slots: [], text };
}

/** One slot per rendered local image, including HTML images which cannot carry image comments. */
export function cellImageSlots(source: string, docPath: string | null): Slot[] {
	return readCell(source, docPath).slots;
}

/**
 * The markdown of the images a cell draws when it draws no text (a status badge, linked or emphasised or not), as offsets into
 * `source`; null when it draws text, holds no local image, or holds one that can't be read back (an HTML `<img>`). The one test of
 * an image-only cell: a press beside its drawn images (`selectionIn`) and its open editor (`widget-cell-comment.ts`) both use it.
 */
export function imageOnlyCell(source: string, docPath: string | null): { from: number; to: number } | null {
	const { slots, text } = readCell(source, docPath);
	if (text || slots.length === 0 || slots.some((slot) => !slot)) return null;
	return { from: slots[0]!.from, to: slots[slots.length - 1]!.to };
}
