/**
 * A stored comment quote is the source text the comment was made on, so a quote taken from a bold or linked passage reads
 * `**Bravo**` or `[the spec](https://…)`. A card shows it as the person saw it: inline marks removed, their text kept.
 * Display only; the stored quote is untouched.
 */
export function plainQuote(source: string): string {
	return source
		.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
		.replace(/`([^`]+)`/g, '$1')
		.replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, '$2')
		.replace(/\*(?=\S)([^*]+?)(?<=\S)\*/g, '$1')
		.replace(/(^|[^\w])_(?=\S)([^_]+?)(?<=\S)_(?=[^\w]|$)/g, '$1$2')
		.replace(/~~(?=\S)(.+?)(?<=\S)~~/g, '$1')
		.replace(/\\([\\`*_{}[\]()#+\-.!|~])/g, '$1')
		.trim();
}
