import DOMPurify from 'dompurify';
import { renderMarkdown } from '$lib/markdown';
import type { MentionOccurrence } from './mentionSelection';
export type TaggedAgent = { id: string; name: string };

/** Render only explicitly identified occurrences; never infer identity from a display name. */
export function renderMentions(text: string, taggedAgents: TaggedAgent[], occurrences: MentionOccurrence[] = []): string {
	if (typeof document === 'undefined') return renderMarkdown(text);
	const agents = new Map(taggedAgents.map(agent => [agent.id, agent]));
	const valid = [...occurrences].sort((a, b) => a.start - b.start).filter((item, index, all) =>
		agents.has(item.agent_id) && Number.isInteger(item.start) && Number.isInteger(item.end) && item.start >= 0 && item.end > item.start && item.end <= text.length && (index === 0 || all[index - 1].end <= item.start));
	let prefix = 'CORSIAMENTIONTOKEN'; while (text.includes(prefix)) prefix += 'X';
	let marked = '', cursor = 0;
	valid.forEach((item, i) => { marked += text.slice(cursor, item.start) + `${prefix}${i}END`; cursor = item.end; });
	marked += text.slice(cursor);
	const root = document.createElement('div'); root.innerHTML = renderMarkdown(marked);
	// A selection can occur in a Markdown link destination; restore it before sanitizing again.
	for (const element of root.querySelectorAll('*')) {
		for (const attribute of [...element.attributes]) {
			const restored = attribute.value.replace(new RegExp(`${prefix}(\\d+)END`, 'g'), (token, index) => {
				const item = valid[Number(index)]; return item ? text.slice(item.start, item.end) : token;
			});
			if (restored !== attribute.value) element.setAttribute(attribute.name, restored);
		}
	}
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	const nodes: Text[] = []; while (walker.nextNode()) nodes.push(walker.currentNode as Text);
	const shown = new Set<string>();
	for (const node of nodes) {
		let remaining = node.data, changed = false; const fragment = document.createDocumentFragment();
		const pattern = new RegExp(`${prefix}(\\d+)END`, 'g'); let match: RegExpExecArray | null, at = 0;
		while ((match = pattern.exec(remaining))) {
			const item = valid[Number(match[1])]; if (!item) continue;
			fragment.append(remaining.slice(at, match.index));
			const value = text.slice(item.start, item.end);
			if (node.parentElement?.closest('code, pre, a')) fragment.append(value);
			else { const span = document.createElement('span'); span.className = 'comment-mention'; span.dataset.agentId = item.agent_id; span.textContent = value; fragment.append(span); shown.add(item.agent_id); }
			at = match.index + match[0].length; changed = true;
		}
		if (changed) { fragment.append(remaining.slice(at)); node.replaceWith(fragment); }
	}
	// Agent-tool tags may have no inline occurrence.
	for (const agent of taggedAgents) {
		if (shown.has(agent.id) || valid.some(item => item.agent_id === agent.id)) continue;
		const span = document.createElement('span'); span.className = 'comment-mention'; span.dataset.agentId = agent.id; span.textContent = `@${agent.name}`;
		if (root.textContent) root.append(' '); root.append(span);
	}
	return DOMPurify.sanitize(root.innerHTML, { ADD_ATTR: ['target'] });
}
