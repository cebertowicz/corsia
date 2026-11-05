type Selection = { id: string; start: number; text: string };

/** Track picker selections through edits; touching a tag removes its identity. */
export function createMentionSelection(initial = '') {
	let previous = initial;
	let selections: Selection[] = [];
	function update(next: string) {
		let start = 0;
		while (start < previous.length && start < next.length && previous[start] === next[start]) start++;
		let oldEnd = previous.length, newEnd = next.length;
		while (oldEnd > start && newEnd > start && previous[oldEnd - 1] === next[newEnd - 1]) { oldEnd--; newEnd--; }
		const shift = newEnd - oldEnd;
		selections = selections.flatMap((item) => {
			const end = item.start + item.text.length;
			if (oldEnd <= item.start) return [{ ...item, start: item.start + shift }];
			if (start >= end) return [item];
			return [];
		});
		previous = next;
	}
	return {
		update,
		clear() { previous = ''; selections = []; },
		select(id: string, start: number, text: string) { selections.push({ id, start, text }); },
		ids(text: string) { update(text); return [...new Set(selections.filter((item) => text.slice(item.start, item.start + item.text.length) === item.text).map((item) => item.id))]; },
	};
}
