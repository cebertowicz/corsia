// A comment tag opens an answer section, as the server's chat_comment_answers._TAGGED reads it.
// Both read the cases in backend/tests/fixtures/chat-answer-tags.json, so the two cannot drift apart.
const CID = 'c[1-9][0-9]*\\.[1-9][0-9]*';
const BOUNDARY = '(?:^|\\r?\\n[ \\t]*\\r?\\n)';
const FIRST_TAG = new RegExp(`${BOUNDARY}[ \\t]*\\[${CID}\\]`);
// A tail that may still become a tag once the next words arrive.
const PARTIAL_TAG = new RegExp(`${BOUNDARY}[ \\t]*(?:\\[(?:c(?:[1-9][0-9]*(?:\\.(?:[1-9][0-9]*)?)?)?)?)?$`);

/** Whether the text holds a tagged answer section. */
export function hasAnswerTag(text: string): boolean {
	return FIRST_TAG.test(text);
}

/**
 * The words of a message still being written that may show in chat: everything before its first
 * answer section, holding back a tail that could become one. It only withholds; the server places
 * the sections, and its row decides what shows once the message is written.
 */
export function visibleStreamText(text: string): string {
	const tag = FIRST_TAG.exec(text) ?? PARTIAL_TAG.exec(text);
	return tag ? text.slice(0, tag.index) : text;
}
