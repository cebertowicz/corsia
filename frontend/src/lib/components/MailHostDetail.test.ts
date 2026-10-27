import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MailHostDetail from './MailHostDetail.svelte';
import { retryMailHost, saveMailHostServers } from '$lib/api/mailHostAccounts';
import { KeyWriteError } from '$lib/stores/keys';
import type { ConnectedAccount, MailHostDetail as Detail } from '$lib/api/connectedApps';

vi.mock('$lib/api/mailHostAccounts', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/mailHostAccounts')>()),
	retryMailHost: vi.fn(async () => {}),
	saveMailHostServers: vi.fn(async () => {}),
}));

const account = (display: string, mailhost: Detail, capabilities: ConnectedAccount['capabilities'] = ['mail', 'calendar']): ConnectedAccount => ({
	kind: 'account', id: display, provider: 'mailhost', provider_display_name: display, address: `you@${display.toLowerCase()}.com`,
	capabilities, state: mailhost.problem === null ? 'connected' : 'needs_sign_in', mailhost, reach: { mode: 'corsia', project_ids: [], outside_projects: false },
});
const unreachable = () => account('example.com', { problem: 'unreachable', server: 'imap.example.com', imap: 'imap.example.com', smtp: 'smtp.example.com', caldav: 'https://mail.example.com/dav', failing: 'imap' });
let host: HTMLElement | null = null;
let component: Record<string, unknown> | null = null;
function render(row: ConnectedAccount, props: Record<string, unknown> = {}) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(MailHostDetail, { target: host, props: { account: row, onSetKey: vi.fn(), onWhere: vi.fn(), onRemove: vi.fn(), ...props } });
	flushSync();
	return host;
}
const button = (root: HTMLElement, name: string) => [...root.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === name);
function type(selector: string, value: string): void {
	const input = host!.querySelector<HTMLInputElement>(selector)!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}
const submit = (root: HTMLElement) => { root.querySelector('form')!.dispatchEvent(new Event('submit', { cancelable: true })); flushSync(); };
afterEach(() => { vi.clearAllMocks(); if (component) unmount(component); component = null; host?.remove(); host = null; });

describe('MailHostDetail', () => {
	it('asks for the app password with Set key as the one primary action', () => {
		const onSetKey = vi.fn();
		const root = render(account('iCloud', { problem: 'waiting' }), { onSetKey });
		expect(root.textContent).toContain('iCloud needs an app password before agents can use this account: make one in your iCloud account settings, then press Set key.');
		expect(root.textContent).not.toContain('press Set.');
		expect(button(root, 'Set key')?.className).toContain('primary');
		expect(root.textContent).not.toContain('Reaches');
		button(root, 'Set key')!.click();
		expect(onSetKey).toHaveBeenCalledOnce();
	});
	it('links the host’s settings page where the person must go next, only when it is known', () => {
		const body = (row: ConnectedAccount) => {
			const root = render(row);
			const result = { text: root.querySelector('.impact')!.textContent, links: [...root.querySelectorAll('.impact a')].map((link) => [link.textContent, link.getAttribute('href'), link.getAttribute('target'), link.getAttribute('rel')]) };
			unmount(component!); component = null; host!.remove(); host = null;
			return result;
		};
		const url = 'https://account.apple.com/';
		expect(body(account('iCloud', { problem: 'waiting', manage_url: url })).links).toEqual([['iCloud account settings', url, '_blank', 'noopener']]);
		expect(body(account('iCloud', { problem: 'refused', manage_url: url })).links).toEqual([['iCloud account settings', url, '_blank', 'noopener']]);
		// manage_url is the account-security page, not the IMAP setting, so the IMAP-off words never link to it.
		expect(body(account('Zoho', { problem: 'imap_off', manage_url: 'https://accounts.zoho.com/', product_name: 'Zoho Mail' })).links).toEqual([]);
		for (const problem of ['waiting', 'refused'] as const) expect(body(account('off.test', { problem })).links).toEqual([]);
		expect(body(account('off.test', { problem: 'waiting' })).text).toBe('off.test needs an app password before agents can use this account: make one in your off.test account settings, then press Set key.');
	});
	it('names the product only for a known host: a typed-in host is sent to its host or IT', () => {
		const known = render(account('Zoho', { problem: 'imap_off', manage_url: 'https://accounts.zoho.com/', product_name: 'Zoho Mail' }));
		expect(known.querySelector('.impact')!.textContent).toBe('Zoho has email access for other apps turned off. Turn on IMAP in Zoho Mail settings, then press Retry.');
		unmount(component!); component = null; host!.remove(); host = null;
		const typed = render(account('off.test', { problem: 'imap_off' }));
		expect(typed.querySelector('.impact')!.textContent).toBe('off.test has email access for other apps turned off. Turn on IMAP with your host or IT, then press Retry.');
		expect(typed.textContent).not.toContain('Mail settings');
	});
	it('offers Replace key when the host refused the password', () => {
		const root = render(account('iCloud', { problem: 'refused' }));
		expect(root.textContent).toContain("iCloud didn’t accept this app password. Make a new one in your iCloud account settings, then press Replace key.");
		expect(button(root, 'Replace key')?.className).toContain('primary');
	});
	it('says only email is connected when the host has no calendar', () => {
		const root = render(account('AOL', { problem: null, calendar_missing: true }, ['mail']));
		expect(root.querySelector('.capabilities')?.textContent).toBe('ReachesEmail');
		expect(root.textContent).toContain("AOL didn’t offer a calendar to CORSIA, so only email is connected.");
		expect(button(root, 'Where it works…')).toBeDefined();
		expect(button(root, 'Sign out')).toBeUndefined();
	});
	it('opens three server fields with the error on the failing one and Save only once changed', async () => {
		const onChanged = vi.fn();
		const root = render(unreachable(), { onChanged });
		expect(root.textContent).toContain('CORSIA couldn’t reach imap.example.com. Check the server names with your host or IT.');
		expect(root.querySelectorAll('input')).toHaveLength(0);
		button(root, 'Edit server')!.click();
		flushSync();
		expect([...root.querySelectorAll('input')].map((input) => input.value)).toEqual(['imap.example.com', 'smtp.example.com', 'https://mail.example.com/dav']);
		expect([...root.querySelectorAll('.err')].map((error) => error.textContent)).toEqual(['CORSIA couldn’t reach this server.']);
		// The failing field shows its error in place of its hint.
		expect([...root.querySelectorAll('.hint')].map((hint) => hint.textContent)).toEqual(['smtp.example.com or smtp.example.com:465', 'Leave empty if your host has no calendar.']);
		expect(button(root, 'Edit server')).toBeUndefined();
		expect(button(root, 'Save')).toBeUndefined();
		expect(button(root, 'Cancel')).toBeDefined();
		// Enter with nothing changed does nothing.
		submit(root);
		expect(saveMailHostServers).not.toHaveBeenCalled();
		type('#mailhost-imap', 'mail.example.com');
		expect(root.querySelectorAll('.err')).toHaveLength(0);
		expect([...root.querySelectorAll('.hint')].map((hint) => hint.textContent)).toEqual(['mail.example.com or mail.example.com:993', 'smtp.example.com or smtp.example.com:465', 'Leave empty if your host has no calendar.', 'The password you sign in to CORSIA with. Needed to change where your app password is sent.']);
		expect(button(root, 'Save')?.className).toContain('primary');
		button(root, 'Cancel')!.click();
		flushSync();
		expect(root.querySelectorAll('input')).toHaveLength(0);
		button(root, 'Edit server')!.click();
		flushSync();
		type('#mailhost-imap', 'mail.example.com');
		submit(root);
		expect(saveMailHostServers).not.toHaveBeenCalled();
		expect(root.querySelector('.err')?.textContent).toBe('Enter your CORSIA password.');
		type('#mailhost-password', 'correct horse');
		submit(root);
		await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
		expect(saveMailHostServers).toHaveBeenCalledWith('example.com', { imap: 'mail.example.com', smtp: 'smtp.example.com', caldav: 'https://mail.example.com/dav' }, 'correct horse');
		expect(root.querySelectorAll('input')).toHaveLength(0);
	});
	it('shows the password field under the server fields only once a value changed, and clears it after a refusal', async () => {
		vi.mocked(saveMailHostServers).mockRejectedValueOnce(new KeyWriteError('wrong_password', 'That password is wrong. Nothing was changed.'));
		const root = render(unreachable());
		button(root, 'Edit server')!.click();
		flushSync();
		expect(root.querySelector('#mailhost-password')).toBeNull();
		type('#mailhost-smtp', 'mail.example.com');
		const field = root.querySelector<HTMLInputElement>('#mailhost-password')!;
		expect(field.type).toBe('password');
		expect(field.autocomplete).toBe('current-password');
		expect(root.querySelector('label[for="mailhost-password"]')?.textContent).toBe('CORSIA password');
		type('#mailhost-password', 'wrong');
		button(root, 'Save')!.click();
		await vi.waitFor(() => expect([...root.querySelectorAll('.err')].map((error) => error.textContent)).toContain('That password is wrong. Nothing was changed.'));
		expect(root.querySelector<HTMLInputElement>('#mailhost-password')!.value).toBe('');
		expect(root.querySelector('form')).not.toBeNull();
	});
	it('shows a refused server name as a line, not on the password field', async () => {
		vi.mocked(saveMailHostServers).mockRejectedValueOnce(new KeyWriteError('invalid_servers', 'That server name isn’t valid.'));
		const root = render(unreachable());
		button(root, 'Edit server')!.click();
		flushSync();
		type('#mailhost-imap', 'not a server');
		type('#mailhost-password', 'right');
		button(root, 'Save')!.click();
		await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe('That server name isn’t valid.'));
		expect([...root.querySelectorAll('.err')].map((error) => error.textContent)).not.toContain('That server name isn’t valid.');
	});
	it('re-checks with one press of Try again: no form, no password, then the row is told a check started', async () => {
		const onChanged = vi.fn();
		const root = render(account('iCloud', { problem: 'imap_off' }), { onChanged });
		expect(root.querySelectorAll('input')).toHaveLength(0);
		expect(button(root, 'Retry')?.className).toContain('primary');
		button(root, 'Retry')!.click();
		await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
		expect(retryMailHost).toHaveBeenCalledOnce();
		expect(vi.mocked(retryMailHost).mock.calls[0]).toEqual(['iCloud']);
		expect(root.querySelectorAll('input')).toHaveLength(0);
	});
	it('presses Try again once even when pressed twice while it is running', async () => {
		let finish: () => void = () => {};
		vi.mocked(retryMailHost).mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
		const onChanged = vi.fn();
		const root = render(unreachable(), { onChanged });
		button(root, 'Retry')!.click();
		button(root, 'Retry')!.click();
		flushSync();
		finish();
		await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
		expect(retryMailHost).toHaveBeenCalledOnce();
	});
	it('shows a refused Try again as a line and does not close the row', async () => {
		vi.mocked(retryMailHost).mockRejectedValueOnce(new KeyWriteError('unknown', 'Couldn’t save that. Nothing was changed.'));
		const onChanged = vi.fn();
		const root = render(unreachable(), { onChanged });
		button(root, 'Retry')!.click();
		await vi.waitFor(() => expect(root.querySelector('[role="alert"]')?.textContent).toBe('Couldn’t save that. Nothing was changed.'));
		expect(onChanged).not.toHaveBeenCalled();
		expect(button(root, 'Retry')).toBeDefined();
	});
	it('shows only Save and Cancel while editing: no Where it works…, no Remove', () => {
		const root = render(unreachable());
		expect(button(root, 'Where it works…')).toBeDefined();
		expect(button(root, 'Remove')).toBeDefined();
		button(root, 'Edit server')!.click();
		flushSync();
		expect(button(root, 'Where it works…')).toBeUndefined();
		expect(button(root, 'Remove')).toBeUndefined();
		expect([...root.querySelectorAll('button')].map((candidate) => candidate.textContent?.trim())).toEqual(['Cancel']);
		type('#mailhost-imap', 'mail.example.com');
		expect([...root.querySelectorAll('button')].map((candidate) => candidate.textContent?.trim())).toEqual(['Save', 'Cancel']);
		button(root, 'Cancel')!.click();
		flushSync();
		expect(button(root, 'Where it works…')).toBeDefined();
		expect(button(root, 'Remove')).toBeDefined();
	});
	it('keeps the typed server values when the CORSIA password is wrong, and clears only the password', async () => {
		vi.mocked(saveMailHostServers).mockRejectedValueOnce(new KeyWriteError('wrong_password', 'That password is wrong. Nothing was changed.'));
		const root = render(unreachable());
		button(root, 'Edit server')!.click();
		flushSync();
		type('#mailhost-imap', 'mail.typed.example');
		type('#mailhost-smtp', 'smtp.typed.example');
		type('#mailhost-caldav', 'https://dav.typed.example/');
		type('#mailhost-password', 'nope');
		button(root, 'Save')!.click();
		await vi.waitFor(() => expect([...root.querySelectorAll('.err')].map((error) => error.textContent)).toContain('That password is wrong. Nothing was changed.'));
		expect(['#mailhost-imap', '#mailhost-smtp', '#mailhost-caldav', '#mailhost-password'].map((id) => root.querySelector<HTMLInputElement>(id)!.value))
			.toEqual(['mail.typed.example', 'smtp.typed.example', 'https://dav.typed.example/', '']);
		expect(button(root, 'Save')).toBeDefined();
	});
	it('pulls a button out to the column edge only when the first action is a ghost button', () => {
		const outdented = (row: ConnectedAccount) => { const root = render(row); const out = root.querySelector('.actions')!.classList.contains('outdent'); unmount(component!); component = null; host!.remove(); host = null; return out; };
		expect(outdented(account('iCloud', { problem: 'waiting' }))).toBe(false);
		expect(outdented(account('iCloud', { problem: 'refused' }))).toBe(false);
		expect(outdented(account('iCloud', { problem: 'imap_off' }))).toBe(false);
		expect(outdented(unreachable())).toBe(false);
		expect(outdented(account('Zoho', { problem: null }))).toBe(true);
		const root = render(unreachable());
		button(root, 'Edit server')!.click();
		flushSync();
		expect(root.querySelector('.actions')!.classList.contains('outdent')).toBe(true);
		type('#mailhost-imap', 'mail.example.com');
		expect(root.querySelector('.actions')!.classList.contains('outdent')).toBe(false);
	});
	it('shows no actions while checking', () => {
		const root = render(account('iCloud', { problem: 'checking' }));
		expect(root.querySelectorAll('button')).toHaveLength(0);
	});
});
