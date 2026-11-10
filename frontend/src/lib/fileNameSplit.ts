/** How many characters before the extension stay visible when a file name is cut in the middle. */
const STEM_TAIL = 8;
/** An extension is a short dot-suffix after a non-empty stem: `.pdf`, `.docx`, never `.verylongsuffix` or a leading dot. */
const EXTENSION = /\.[A-Za-z0-9]{1,8}$/;

/**
 * Splits a file name into the part that gives way and the part that stays: the end of the stem and the extension
 * (`north-trailhead-lot-…-final-v7.pdf` keeps `final-v7.pdf`). The caller sets the two parts in one line, the head
 * with an ellipsis that fires only when the line is too narrow, so the cut follows the width the chip really has.
 * `head + tail` is always the name. A name that is already no longer than its tail comes back whole as the head.
 */
export function splitFileName(name: string): { head: string; tail: string } {
	const chars = Array.from(name);
	const extension = EXTENSION.exec(name);
	const extLength = extension && extension.index > 0 ? Array.from(extension[0]).length : 0;
	const stemLength = chars.length - extLength;
	const tailLength = extLength + Math.min(STEM_TAIL, stemLength);
	if (chars.length <= tailLength + 1) return { head: name, tail: '' };
	return { head: chars.slice(0, chars.length - tailLength).join(''), tail: chars.slice(chars.length - tailLength).join('') };
}
