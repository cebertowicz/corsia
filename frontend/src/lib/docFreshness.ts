export type FreshnessState =
	| 'current'
	| 'overdue'
	| 'never_verified'
	| 'as_of'
	| 'draft_expiring'
	| 'archived';

export interface DocumentFreshness {
	class: string;
	verified_at: string | null;
	age_days: number | null;
	due_at: string | null;
	state: FreshnessState;
	owner_name: string;
}

export interface FreshnessPresentation {
	phrase: string;
	datetime: string | null;
	dateText: string | null;
	overdue: boolean;
}

function localShortDate(value: string): string {
	return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' }).format(new Date(value));
}

function relativeAge(days: number): string {
	if (days === 0) return 'verified today';
	return `verified ${days} ${days === 1 ? 'day' : 'days'} ago`;
}

/** Render the human-facing freshness copy from the structured API payload. */
export function formatFreshness(value: DocumentFreshness): FreshnessPresentation {
	if (value.state === 'never_verified') {
		return { phrase: 'not yet verified', datetime: null, dateText: null, overdue: false };
	}
	if (value.state === 'as_of') {
		const date = value.verified_at ? localShortDate(value.verified_at) : '';
		return {
			phrase: date ? `as of ${date}` : 'as of an earlier date',
			datetime: value.verified_at,
			dateText: date || null,
			overdue: false,
		};
	}
	if (value.state === 'archived') {
		return { phrase: 'archived', datetime: value.verified_at, dateText: value.verified_at ? localShortDate(value.verified_at) : null, overdue: false };
	}
	const age = value.age_days ?? 0;
	const phrase = relativeAge(age);
	const date = value.verified_at ? localShortDate(value.verified_at) : null;
	return {
		phrase,
		datetime: value.verified_at,
		dateText: date,
		overdue: value.state === 'overdue',
	};
}

export function shouldOfferVerification(value: DocumentFreshness): boolean {
	if (value.state === 'overdue' || value.state === 'never_verified') return true;
	if (!value.due_at) return false;
	const msUntilDue = new Date(value.due_at).getTime() - Date.now();
	return msUntilDue >= 0 && msUntilDue <= 7 * 24 * 60 * 60 * 1000;
}
