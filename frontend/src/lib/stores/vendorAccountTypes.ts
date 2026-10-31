import type { ModelUsage } from './modelUsage';

export type VendorAccountState = 'signed_in' | 'signed_out' | 'removed';
export type SigninStatus = 'waiting' | 'completed' | 'already_signed_in' | 'model_refresh_failed' | 'failed' | 'refused' | 'cancelled' | 'superseded' | 'unavailable';

export interface VendorAccountLabel {
	name: string;
	email: string | null;
	text: string;
	custom: string | null;
}

/** An account's recorded subscription price; the account's billing is null when none is recorded. */
export interface AccountBilling {
	monthly_price_cents: number;
	charge_day: number;
}

export interface VendorAccountUsage extends ModelUsage {
	/** A background account-usage recheck is still running. */
	checking: boolean;
	/** The most recent usage check, including any failure the account row should show. */
	check: { outcome?: string; at?: string; error?: string | null } | null;
}

export interface VendorAccount {
	id: string;
	provider: string;
	name: string;
	is_default: boolean;
	state: VendorAccountState;
	revision: number;
	identity: Record<string, string>;
	label: VendorAccountLabel;
	duplicate_of?: string | null;
	/** This account's 'Spend past usage limit' switch; null where no plan limit applies. */
	allow_overage?: boolean | null;
	/** Signed in, but none of the vendor's models can run on this account. */
	no_usable_model?: boolean;
	/** Set on the converted old Default until its first sign-in in CORSIA: its signed-out row says why. */
	upgrade_reason?: string | null;
	/** Account-owned plan usage, rendered once in its Settings account row. */
	usage?: VendorAccountUsage;
	/** The recorded monthly price and charge day; null when no price is recorded. */
	billing?: AccountBilling | null;
	/** Fences billing writes; sent back unchanged. */
	billing_revision?: number;
	/** A receipt confirmed state, but account details/revision require an explicit reload. */
	details_stale?: boolean;
}

export interface SigninResult {
	account_id: string;
	route: 'browser' | null;
	status: SigninStatus;
	cause: string | null;
	duplicate_of?: string | null;
	held_by?: string | null;
	/** Support reference for a failure the person may report. */
	ref?: string | null;
}
