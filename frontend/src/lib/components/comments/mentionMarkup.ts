import DOMPurify from 'dompurify';
import { renderMarkdown } from '$lib/markdown';
import type { MentionOccurrence } from './mentionSelection';
export type TaggedAgent = { id: string; name: string };

/** Occurrences in order and in bounds, with no overlap: the ones the comment can show. */
function placeable(text: string, occurrences: MentionOccurrence[], known: (id: string) => boolean) {
	return [...occurrences].sort((a, b) => a.start - b.start).filter((item, index, all) =>
		known(item.agent_id) && Number.isInteger(item.start) && Number.isInteger(item.end) && item.start >= 0 && item.end > item.start && item.end <= text.length && (index === 0 || all[index - 1].end <= item.start));
}

/** The comment's Markdown rendered with each occurrence as a numbered token (`valid[n]`), and its text nodes. */
function tokenized(text: string, valid: MentionOccurrence[]) {
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
	return { root, prefix, nodes };
}

/** Code, preformatted text and links show an occurrence as plain text, so it reads as no tag. */
const plainHere = (node: Text) => !!node.parentElement?.closest('code, pre, a');

/** The occurrences a reader sees as tags: those outside code, preformatted text, links and link destinations. */
export function shownAsTags(text: string, occurrences: MentionOccurrence[]): MentionOccurrence[] {
	if (typeof document === 'undefined') return occurrences;
	const valid = placeable(text, occurrences, () => true);
	const { prefix, nodes } = tokenized(text, valid);
	const shown: MentionOccurrence[] = [];
	for (const node of nodes) {
		if (plainHere(node)) continue;
		for (const match of node.data.matchAll(new RegExp(`${prefix}(\\d+)END`, 'g'))) { const item = valid[Number(match[1])]; if (item) shown.push(item); }
	}
	return shown.sort((a, b) => a.start - b.start);
}

/** Render only explicitly identified occurrences; never infer identity from a display name. */
export function renderMentions(
	text: string, taggedAgents: TaggedAgent[], occurrences: MentionOccurrence[] = [], taggedPerson: string | null = null,
): string {
	if (typeof document === 'undefined') return renderMarkdown(text);
	const agents = new Map(taggedAgents.map(agent => [agent.id, agent]));
	const valid = placeable(text, occurrences, id => agents.has(id));
	const { root, prefix, nodes } = tokenized(text, valid);
	const shown = new Set<string>();
	for (const node of nodes) {
		let remaining = node.data, changed = false; const fragment = document.createDocumentFragment();
		const pattern = new RegExp(`${prefix}(\\d+)END`, 'g'); let match: RegExpExecArray | null, at = 0;
		while ((match = pattern.exec(remaining))) {
			const item = valid[Number(match[1])]; if (!item) continue;
			fragment.append(remaining.slice(at, match.index));
			const value = text.slice(item.start, item.end);
			if (plainHere(node)) fragment.append(value);
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
	// A tag of the person is not an agent tag: it marks the comment, and carries no agent identity.
	if (taggedPerson) {
		const span = document.createElement('span'); span.className = 'comment-mention'; span.dataset.personTag = '1'; span.textContent = `@${taggedPerson}`;
		if (root.textContent) root.append(' '); root.append(span);
	}
	return DOMPurify.sanitize(root.innerHTML, { ADD_ATTR: ['target'] });
}
