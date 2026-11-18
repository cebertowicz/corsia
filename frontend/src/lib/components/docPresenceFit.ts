/** Prefer the whole document name before showing an individual participant. */
export function presenceMaximum({ budget, fullStackWidth, singleWidth, countWidth, fullTitleWidth, floorTitleWidth, singleOverlaps }: {
	budget: number; fullStackWidth: number; singleWidth: number; countWidth: number;
	fullTitleWidth: number; floorTitleWidth: number; singleOverlaps: boolean;
}): 3 | 1 | 0 {
	if (budget - fullStackWidth >= fullTitleWidth) return 3;
	if (!singleOverlaps && budget - singleWidth >= fullTitleWidth) return 1;
	if (budget - countWidth >= fullTitleWidth) return 0;
	return !singleOverlaps && budget - singleWidth >= floorTitleWidth ? 1 : 0;
}
