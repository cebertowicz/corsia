/** Keep the whole document name before expanding the participant stack. */
export function presenceMaximum(budget: number, fullStackWidth: number, singleWidth: number, fullTitleWidth: number, floorTitleWidth: number, singleOverlaps: boolean): 3 | 1 | 0 {
	if (budget - fullStackWidth >= fullTitleWidth) return 3;
	return !singleOverlaps && budget - singleWidth >= floorTitleWidth ? 1 : 0;
}
