/** Move a partly clipped Agreed line entirely into or out of view without hiding the reply. */
export function agreeLineShift(top: number, bottom: number, agreed: DOMRect, field: DOMRect, recipient: DOMRect): number {
	if (agreed.bottom <= top || agreed.top >= bottom || (agreed.top >= top && agreed.bottom <= bottom)) return 0;
	const replyFits = (shift: number) => Math.min(field.top, recipient.top) - shift >= top && Math.max(field.bottom, recipient.bottom) - shift <= bottom;
	const show = agreed.top < top ? agreed.top - top : agreed.bottom - bottom;
	if (agreed.top - show >= top && agreed.bottom - show <= bottom && replyFits(show)) return show;
	const hide = [agreed.bottom - top, agreed.top - bottom].filter(replyFits).sort((a, b) => Math.abs(a) - Math.abs(b));
	return hide[0] ?? 0;
}
