/** Remove only sections the server placed in chat comment threads. */
export function visibleChatAnswerText(text: string, cids: readonly string[]): string {
	if (!cids.length) return text;
	const pattern = /(?:^|\r?\n[ \t]*\r?\n)([ \t]*(?:\[c[1-9][0-9]*\.[1-9][0-9]*\][ \t]*)+)/g;
	const matches = [...text.matchAll(pattern)];
	const known = new Set(cids);
	let result = text.slice(0, matches[0]?.index ?? text.length);
	for (let index = 0; index < matches.length; index++) {
		const match = matches[index];
		const end = matches[index + 1]?.index ?? text.length;
		const tags = [...match[1].matchAll(/\[(c[1-9][0-9]*\.[1-9][0-9]*)\]/g)].map(tag => tag[1]);
		if (!tags.some(tag => known.has(tag))) result += text.slice(match.index!, end);
	}
	return result.trim();
}
