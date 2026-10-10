export interface UsageWindow {
	kind: string;
	state: 'reported' | 'unknown';
	source?: string;
	observed_at?: string | null;
	resets_at?: string | null;
	used_percent?: number | null;
	/** Reset of the window `used_percent` was read from, when it differs from `resets_at`. */
	used_percent_resets_at?: string | null;
	window_minutes?: number | null;
	scope_model?: string | null;
}

/** The connection usage projection a harness row carries from `/api/models`. */
export interface ModelUsage {
	state: 'allowed' | 'limited' | 'unknown';
	usable: boolean;
	/** Why the row is limited; `plan_limit_overage_off` while the overage switch holds it. */
	reason?: string | null;
	resets_at?: string | null;
	windows: UsageWindow[];
	/** The account's 'Spend past plan limit' switch; off by default. */
	allow_overage?: boolean;
	/** Provider facts that establish paid capacity after an exhausted plan window. */
	facts?: Record<string, unknown>;
}
