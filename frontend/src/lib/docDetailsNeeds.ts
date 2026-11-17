/** What needs the reader behind the document's Details button and is shown nowhere else on screen. Each source is one flag, so a
 * new source is a new required field here, and the unit test that sums these flags stops compiling until it names it. */
export type DetailsNeeds = {
	/** The document is past its verification date. */
	overdue: boolean;
	/** File changed, where the app bar does not show it (the canvas is wide enough to split). */
	fileChangedOffBar: boolean;
	/** An ask whose card is hidden (under the comment sheet). */
	hiddenAsk: boolean;
};

/** The amber count beside Details: one per flag that is set. */
export function detailsNeedsCount(needs: DetailsNeeds): number {
	return (needs.overdue ? 1 : 0) + (needs.fileChangedOffBar ? 1 : 0) + (needs.hiddenAsk ? 1 : 0);
}
