export type TaggedAgent = { id: string; name: string };

/** Decorate inline tags and show tags added through the agent comment tool. */
export function colorMentions(html: string, taggedAgents: TaggedAgent[]): string {
	if (typeof document === 'undefined' || !taggedAgents.length) return html;
	const root = document.createElement('div'); root.innerHTML = html;
	const agents = [...taggedAgents].sort((a, b) => b.name.length - a.name.length);
	const foundIds = new Set<string>();
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	const nodes: Text[] = []; while (walker.nextNode()) nodes.push(walker.currentNode as Text);
	for (const node of nodes) {
		if (node.parentElement?.closest('code, pre, a')) continue;
		const text = node.data; let cursor = 0, changed = false; const fragment = document.createDocumentFragment();
		while (cursor < text.length) {
			const at = text.indexOf('@', cursor); if (at < 0) break;
			const found = agents.find((a) => text.startsWith(a.name, at + 1) && !/[\p{L}\p{N}_]/u.test(text[at + 1 + a.name.length] ?? ''));
			if (!found) { fragment.append(text.slice(cursor, at + 1)); cursor = at + 1; continue; }
			fragment.append(text.slice(cursor, at)); const span = document.createElement('span'); span.className = 'comment-mention'; span.textContent = `@${found.name}`; fragment.append(span); foundIds.add(found.id); cursor = at + found.name.length + 1; changed = true;
		}
		if (changed) { fragment.append(text.slice(cursor)); node.replaceWith(fragment); }
		}
	for (const agent of taggedAgents) {
		if (foundIds.has(agent.id)) continue;
		const span = document.createElement('span'); span.className = 'comment-mention'; span.textContent = `@${agent.name}`;
		if (root.textContent) root.append(' ');
		root.append(span); foundIds.add(agent.id);
	}
	return root.innerHTML;
}
