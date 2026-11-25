/** What needs the reader behind the document's Details button and is shown nowhere else on screen. Each source is one flag, so a
 * new source is a new required field here, and the unit test that reads these flags stops compiling until it names it. */
export type DetailsNeeds = {
	/** The document is past its verification date. */
	overdue: boolean;
	/** An ask whose card is hidden (under the comment sheet). */
	hiddenAsk: boolean;
	/** Comments waiting for the reader's decision, whose Dismiss all row is only reachable by opening Details. */
	hiddenTask: boolean;
};

/** What each source is called, in the words the Details panel itself uses for that entry. Keyed by the same required
 * fields as the flags, so a new source does not compile until it says what it is called. */
export type DetailsNeedsWords = { [source in keyof DetailsNeeds]: string };

/** The sources that are up, each named by its own words. The amber count is this list's length and the control's accessible
 * name reads the list itself, so what the count says and what the name says can't drift apart. */
export function detailsNeedsUp(needs: DetailsNeeds, words: DetailsNeedsWords): string[] {
	return (Object.keys(needs) as (keyof DetailsNeeds)[]).filter((source) => needs[source]).map((source) => words[source]);
}
