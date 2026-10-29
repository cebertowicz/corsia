import { tick } from 'svelte';
import type { MenuItem } from '../ui/Menu.svelte';
import { VENDORS } from '$lib/conductorSources';
import type { VendorAccount } from '$lib/stores/vendorAccounts';

export function accountMenuItems(account: VendorAccount | undefined, pending: Record<string, boolean>): MenuItem[] {
	if (!account) return [];
	const disabled = account.details_stale || !!pending[account.id];
	return [
		{ id: 'rename', label: 'Rename', disabled },
		...(account.state === 'legacy' || account.is_default ? [] : [{ id: 'sign-in-again', label: 'Sign in again', disabled }]),
		...(account.state === 'legacy' ? [] : [
			{ id: 'sign-out', label: 'Sign out', disabled },
			{ id: 'remove', label: 'Remove', danger: true, disabled },
		]),
	];
}

export const displayName = (account: VendorAccount): string => account.label.text;

export function accountStateLabel(account: VendorAccount): string | null {
	if (account.state === 'signed_out') return 'Signed out';
	if (account.state === 'legacy') return 'Managed by provider';
	if (account.state === 'removed') return 'Removed';
	const vendor = VENDORS[account.provider];
	if (account.no_usable_model && vendor) return `This ${vendor.name} account has no models for ${vendor.cli}. Sign in with another account, or turn this account off.`;
	return null;
}

export async function landOnAccount(account: VendorAccount, focusRow: (id: string) => void, openMenu: (row: HTMLElement) => void) {
	await tick();
	const row = document.getElementById(`vendor-account-${account.id}`);
	if (!row) return;
	row.scrollIntoView({ block: 'center' });
	if (account.state === 'signed_out') {
		const button = row.querySelector<HTMLButtonElement>('[data-link-target] button');
		if (button && !button.disabled) {
			button.focus();
			if (document.activeElement === button) focusRow(account.id);
			return;
		}
	} else if (account.state === 'signed_in' && !account.is_default) {
		openMenu(row);
		return;
	}
	row.focus();
}
