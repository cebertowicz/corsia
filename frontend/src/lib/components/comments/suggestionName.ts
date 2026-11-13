/** Longest quoted text in a suggestion's name; a longer one is cut with an ellipsis so the name stays one short phrase. */
const LONGEST = 40;

const clip = (text: string) => {
	const flat = text.replace(/\s+/g, ' ').trim();
	return flat.length > LONGEST ? `${flat.slice(0, LONGEST - 1).trimEnd()}…` : flat;
};

/**
 * What a suggestion changes, in a few words: the accessible name of its Accept and Reject says which suggestion a tap
 * decides ("Accept: Step 1 to Phase 1"). Cards in a list otherwise expose identical controls.
 */
export function suggestionName(anchor: string, replacement: string, insertAfter = false): string {
	const to = clip(replacement);
	if (insertAfter) return to ? `insert ${to}` : 'an insertion';
	const from = clip(anchor);
	if (!to) return from ? `delete ${from}` : 'a deletion';
	return from ? `${from} to ${to}` : `insert ${to}`;
}
