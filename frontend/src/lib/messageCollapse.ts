export const LONG_MESSAGE_CHAR_THRESHOLD = 900;
export const LONG_MESSAGE_LINE_THRESHOLD = 12;

export function isLongMessageText(value: string | null | undefined): boolean {
	if (!value) return false;
	return value.length > LONG_MESSAGE_CHAR_THRESHOLD
		|| value.split(/\r\n|\r|\n/).length > LONG_MESSAGE_LINE_THRESHOLD;
}
