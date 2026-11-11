import { hashSeed } from '$lib/typingPace';

export type MentionOccurrence = { agent_id: string; start: number; end: number };
type Selection = { id: string; start: number; text: string };

/** At send, a typed `@Name` naming exactly one taggable agent tags it, as if picked; the stored occurrence carries the id.
 *  Returns every occurrence, in text order. The text decides: the longest name wins, so a picked `@Agent` that now reads
 *  `@Agent Substrate` tags Agent Substrate. A picked occurrence otherwise stands, which is how a name two agents share
 *  gets tagged. Offsets are UTF-16 code units of `text`; names compare case-insensitively without changing its length. */
export function withTypedMentions(text: string, agents: { id: string; name: string }[], picked: MentionOccurrence[]): MentionOccurrence[] {
	const byName = new Map<string, { name: string; ids: Set<string> }>();
	for (const agent of agents) {
		const key = agent.name.toLowerCase();
		byName.set(key, { name: agent.name, ids: new Set([...(byName.get(key)?.ids ?? []), agent.id]) });
	}
	const names = [...byName.values()].filter(({ ids }) => ids.size === 1).sort((a, b) => b.name.length - a.name.length);
	let kept = [...picked];
	const found: MentionOccurrence[] = [];
	for (let at = text.indexOf('@'); at >= 0; at = text.indexOf('@', at + 1)) {
		if (at > 0 && !/\s/.test(text[at - 1])) continue;
		const match = names.find(({ name }) => text.slice(at + 1, at + 1 + name.length).toLowerCase() === name.toLowerCase()
			&& !/[\p{L}\p{N}_]/u.test(text[at + 1 + name.length] ?? ''));
		if (!match) continue;
		const end = at + 1 + match.name.length;
		const overlapping = [...kept, ...found].filter(item => item.start < end && at < item.end);
		if (!overlapping.every(item => item.start === at && item.end < end)) continue;
		kept = kept.filter(item => !overlapping.includes(item));
		found.push({ agent_id: [...match.ids][0], start: at, end });
	}
	return [...kept, ...found].sort((a, b) => a.start - b.start);
}

/** Identifies a draft's text by its length and hash, so a snapshot stays the same size however long the draft grows. */
const textKey = (text: string) => `${text.length}:${hashSeed(text)}`;

/** The text and its selected identities share a lifetime. Offsets are UTF-16 code units. */
export function createMentionSelection(initial = '') {
	let previous = initial;
	let selections: Selection[] = [];
	let snapshots: { key: string; selections: Selection[] }[] = [];
	function remember() { snapshots.push({ key: textKey(previous), selections: selections.map(item => ({ ...item })) }); }
	let pending: { start: number; end: number; direction?: 'backward' | 'forward' } | null = null;
	let historyEdit = false;
	function begin(start: number, end: number, inputType: string) {
		historyEdit = inputType === 'historyUndo' || inputType === 'historyRedo';
		if (historyEdit) { pending = null; return; }
		let direction: 'backward' | 'forward' | undefined;
		if (inputType.startsWith('delete') && start === end) {
			if (inputType.endsWith('Backward')) direction = 'backward';
			else if (inputType.endsWith('Forward')) direction = 'forward';
			else { pending = null; return; }
		}
		pending = { start, end, direction };
	}
	function update(next: string) {
		if (next === previous && (!pending || pending.start === pending.end)) { pending = null; historyEdit = false; return; }
		const edit = pending; pending = null;
		const history = historyEdit; historyEdit = false;
		if (edit?.direction) {
			const removed = previous.length - next.length;
			if (removed > 0) {
				if (edit.direction === 'backward') edit.start = Math.max(0, edit.start - removed);
				else edit.end = Math.min(previous.length, edit.end + removed);
			}
		}
		if (!edit || !next.startsWith(previous.slice(0, edit.start)) || !next.endsWith(previous.slice(edit.end))) {
			// Undo can restore a known state, but identical text with different identities is ambiguous.
			const key = textKey(next);
			const candidates = history ? snapshots.filter(snapshot => snapshot.key === key) : [];
			const identities = new Map(candidates.map(snapshot => [JSON.stringify(snapshot.selections), snapshot.selections]));
			selections = identities.size === 1 ? [...identities.values()][0].map(item => ({ ...item })) : [];
			previous = next; remember(); return;
		}
		const inserted = next.length - (previous.length - (edit.end - edit.start));
		if (inserted < 0) { selections = []; previous = next; return; }
		const shift = inserted - (edit.end - edit.start);
		selections = selections.flatMap(item => {
			const end = item.start + item.text.length;
			if (edit.end <= item.start) return [{ ...item, start: item.start + shift }];
			if (edit.start >= end) return [item];
			return [];
		});
		previous = next; remember();
	}
	return {
		begin, update,
		clear() { previous = ''; selections = []; snapshots = []; pending = null; historyEdit = false; },
		select(id: string, start: number, text: string) { selections.push({ id, start, text }); if (snapshots.at(-1)?.key === textKey(previous)) snapshots.pop(); remember(); },
		occurrences(text: string): MentionOccurrence[] {
			update(text);
			return selections.filter(item => text.slice(item.start, item.start + item.text.length) === item.text)
				.map(item => ({ agent_id: item.id, start: item.start, end: item.start + item.text.length }));
		},
	};
}
