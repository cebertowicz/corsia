/**
 * A stored comment quote is the source text the comment was made on, so a quote taken from a bold or linked passage reads
 * `**Bravo**` or `[the spec](https://…)`. A card shows it as the person saw it: inline marks removed, their text kept.
 * A code span is drawn as written, so only its backticks go: its `*`, `_` and `\` stay. Display only; the stored quote is untouched.
 */
export function plainQuote(source: string): string {
	const code: string[] = [];
	const held = source.replace(/(?<!`)(`+)(?!`)([\s\S]*?[^`])\1(?!`)/g, (_, _ticks, text: string) => `${code.push(text) - 1}`);
	return held
		.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, '$2')
		.replace(/\*(?=\S)([^*]+?)(?<=\S)\*/g, '$1')
		.replace(/(^|[^\w])_(?=\S)([^_]+?)(?<=\S)_(?=[^\w]|$)/g, '$1$2')
		.replace(/~~(?=\S)(.+?)(?<=\S)~~/g, '$1')
		.replace(/\\([\\`*_{}[\]()#+\-.!|~])/g, '$1')
		.replace(/(\d+)/g, (_, index: string) => code[Number(index)])
		.trim();
}
