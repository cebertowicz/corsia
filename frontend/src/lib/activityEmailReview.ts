/** Connects the Activity email action row to the existing review note flow. */
export const ACTIVITY_EMAIL_REVIEW = Symbol('activityEmailReview');

export interface ActivityEmailReview {
	readonly enabled: boolean;
	readonly noting: boolean;
	requestChanges: () => void;
	registerRequest: (focus: () => void) => () => void;
	focusRequest: () => void;
}
