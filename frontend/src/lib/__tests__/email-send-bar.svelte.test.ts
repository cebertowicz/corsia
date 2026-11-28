import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import EmailSendBar from '$lib/components/EmailSendBar.svelte';
import { fetchEmailDraftContext } from '$lib/api/emailDrafts';
import { EMAIL_DRAFT_SETTLED } from '$lib/emailDraft';
import { ACTIVITY_EMAIL_REVIEW } from '$lib/activityEmailReview';
import { get } from 'svelte/store';
import { emailDraftHeaders } from '$lib/stores/emailDraftHeaders';

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('$lib/api/request', () => ({ apiFetch }));
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const response = (state: string, status = 200, message = '') => ({ ok: status === 200, status, json: async () => status === 409 ? { detail: { state, message } } : { state } });
const button = (name: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name);
const press = (name: string) => { button(name)!.click(); flushSync(); };
const render = (props: Record<string, unknown> = {}) => { component = mount(EmailSendBar, { target: host, props: { artifactId: 'a/1', text: () => 'To: a@example.com\n\nExact ✉\n', targetVersion: 'shown-target', readAt: 'read-time', ...props } }); flushSync(); };
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); flushSync(); };
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); apiFetch.mockReset(); emailDraftHeaders.set({}); });
afterEach(() => { if (component) unmount(component); component = null; host.remove(); });

describe('email send bar', () => {
	it.each([
		['send', 'sent', false], ['discard', 'discarded', false],
		['send', 'sent', true], ['discard', 'discarded', true], ['send', 'sending', true],
	] as const)('preserves %s state %s after reconstruction (recovery: %s)', async (action, state, recovery) => {
		emailDraftHeaders.set({ 'a/1': { id: 'd', state: 'open', header: { text: '', recipients: [] } } });
		if (recovery) apiFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
		else apiFetch.mockResolvedValue(response(state));
		render({ draftId: 'd', readState: vi.fn().mockResolvedValue(state) });
		if (action === 'send') press('Send now'); else { press('Don’t send'); press('Don’t send'); }
		await settle();
		expect(get(emailDraftHeaders)['a/1'].state).toBe(state);
		unmount(component!); host.replaceChildren();
		render({ draftId: 'd', initialState: get(emailDraftHeaders)['a/1'].state });
		expect(button('Send now')).toBeUndefined();
	});
	it('places the existing review action before the email actions in Activity', () => {
		const requestChanges = vi.fn();
		component = mount(EmailSendBar, { target: host, context: new Map([[ACTIVITY_EMAIL_REVIEW, { enabled: true, noting: false, requestChanges, registerRequest: () => () => {}, focusRequest: () => {} }]]), props: { artifactId: 'a', text: () => '' } }); flushSync();
		expect([...host.querySelectorAll('button')].map((b) => b.textContent?.trim())).toEqual(['Send back', 'Don’t send', 'Send now']);
		press('Send back'); expect(requestChanges).toHaveBeenCalledTimes(1); expect(apiFetch).not.toHaveBeenCalled();
	});
	it('acknowledges an unconfirmed Activity draft and reloads only after 204', async () => {
		const settled = vi.fn();
		apiFetch.mockResolvedValue({ status: 204 });
		component = mount(EmailSendBar, { target: host, context: new Map([[EMAIL_DRAFT_SETTLED, settled]]), props: { artifactId: 'a', draftId: 'd/1', text: () => '', initialState: 'sending' } }); flushSync();
		press('Done'); await settle();
		expect(apiFetch).toHaveBeenCalledWith('/api/attention/drafts/d%2F1/acknowledge', { method: 'POST' });
		expect(settled).toHaveBeenCalledTimes(1);
	});
	it.each([404, 409, 'network'])('keeps the unconfirmed Activity draft visible when acknowledgement fails: %s', async (failure) => {
		const settled = vi.fn();
		if (failure === 'network') apiFetch.mockRejectedValue(new Error('offline'));
		else apiFetch.mockResolvedValue({ status: failure });
		component = mount(EmailSendBar, { target: host, context: new Map([[EMAIL_DRAFT_SETTLED, settled]]), props: { artifactId: 'a', draftId: 'd', text: () => '', initialState: 'sending' } }); flushSync();
		press('Done'); await settle();
		expect(host.textContent).toContain('Couldn’t confirm the email left.');
		expect(host.textContent).toContain('Couldn’t mark this done.');
		expect(button('Done')).toBeTruthy(); expect(settled).not.toHaveBeenCalled();
	});
	it('retains the context read time and state from the server', async () => {
		apiFetch.mockResolvedValue({ ok: true, json: async () => ({ status: 'ready', state: 'open', read_at: 'server-time', conversation: [], attachments: [], notes: [] }) });
		expect(await fetchEmailDraftContext('a')).toMatchObject({ state: 'open', readAt: 'server-time' });
	});
	it('posts the exact editor text, shown target and read time once on a double click, then shows Sent', async () => {
		let resolve!: (value: unknown) => void;
		apiFetch.mockReturnValue(new Promise((r) => { resolve = r; }));
		render(); const send = button('Send now')!; send.click(); send.click(); flushSync(); await settle();
		expect(apiFetch).toHaveBeenCalledTimes(1);
		expect(apiFetch).toHaveBeenCalledWith('/api/email-drafts/a%2F1/send', expect.objectContaining({ method: 'POST', body: JSON.stringify({ text: 'To: a@example.com\n\nExact ✉\n', target_version: 'shown-target', read_at: 'read-time' }) }));
		expect(host.textContent).toContain('Sending…'); expect(host.querySelectorAll('button')).toHaveLength(0);
		resolve(response('sent')); await settle(); expect(host.textContent?.trim()).toBe('Sent');
	});
	it('names addresses the mail host did not deliver to after sending', async () => {
		apiFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ state: 'sent', not_delivered_to: ['lee@example.com', 'sam@example.com'] }) });
		render(); press('Send now'); await settle(); expect(host.textContent?.trim()).toBe('Sent. lee@example.com, sam@example.com didn’t get it.'); expect(host.querySelectorAll('button')).toHaveLength(0);
	});
	it('confirms discard, keeps a draft on cancel, and sends the discard', async () => {
		apiFetch.mockResolvedValue(response('discarded')); render(); press('Don’t send'); expect(host.textContent).toContain('Don’t send this email?'); press('Keep draft'); press('Don’t send'); press('Don’t send'); await settle();
		expect(apiFetch).toHaveBeenCalledWith('/api/email-drafts/a%2F1/discard', expect.objectContaining({ method: 'POST' })); expect(host.textContent).toContain('Not sent. This draft is marked as not to send.');
	});
	it('offers retry after failure and succeeds', async () => {
		apiFetch.mockResolvedValueOnce(response('failed')).mockResolvedValueOnce(response('sent')); render(); press('Send now'); await settle(); expect(host.textContent).toContain('Couldn’t send. The email didn’t leave, and the draft is kept.'); press('Retry'); await settle(); expect(host.textContent?.trim()).toBe('Sent');
	});
	it('reads the stored state when the route answers 500 after the email went', async () => {
		apiFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) });
		const readState = vi.fn().mockResolvedValue('sent');
		render({ readState }); press('Send now'); await settle();
		expect(readState).toHaveBeenCalledTimes(1); expect(host.textContent?.trim()).toBe('Sent'); expect(host.querySelectorAll('button')).toHaveLength(0);
	});
	it('says the email didn’t leave after a dropped connection only when the stored draft is still open', async () => {
		apiFetch.mockRejectedValue(new Error('offline'));
		render({ readState: vi.fn().mockResolvedValue('open') }); press('Send now'); await settle();
		expect(host.textContent).toContain('The email didn’t leave'); expect(button('Retry')).toBeTruthy();
	});
	it('claims nothing when neither the send nor the state read answers', async () => {
		apiFetch.mockRejectedValue(new Error('offline'));
		render({ readState: vi.fn().mockResolvedValue(null) }); press('Send now'); await settle();
		expect(host.textContent).not.toContain('didn’t leave'); expect(host.textContent).toContain('Couldn’t confirm the email left.'); expect(host.querySelectorAll('button')).toHaveLength(0);
	});
	const noAnswer = { 500: () => apiFetch.mockResolvedValue({ ok: false, status: 500, json: async () => ({}) }), thrown: () => apiFetch.mockRejectedValue(new Error('offline')) };
	it.each(['500', 'thrown'] as const)('after a Don’t send with no answer (%s), an open draft goes back to the draft with no Retry and nothing sent', async (how) => {
		noAnswer[how](); const readState = vi.fn().mockResolvedValue('open');
		render({ readState }); press('Don’t send'); press('Don’t send'); await settle();
		expect(readState).toHaveBeenCalledTimes(1);
		expect(host.textContent).toContain('Don’t send didn’t go through. The draft is still open.');
		expect(host.textContent).not.toContain('Couldn’t send'); expect(button('Retry')).toBeUndefined();
		expect(apiFetch).toHaveBeenCalledTimes(1); expect(apiFetch).toHaveBeenCalledWith('/api/email-drafts/a%2F1/discard', expect.anything());
	});
	it.each(['500', 'thrown'] as const)('after a Don’t send with no answer (%s), a stored discard shows Not sent and an unread state claims nothing about the email', async (how) => {
		noAnswer[how](); render({ readState: vi.fn().mockResolvedValue('discarded') }); press('Don’t send'); press('Don’t send'); await settle();
		expect(host.textContent).toContain('Not sent. This draft is marked as not to send.');
		unmount(component!); host.replaceChildren();
		noAnswer[how](); render({ readState: vi.fn().mockResolvedValue(null) }); press('Don’t send'); press('Don’t send'); await settle();
		expect(host.textContent).toContain('Couldn’t confirm Don’t send went through.');
		expect(host.textContent).not.toContain('email left'); expect(host.textContent).not.toContain('Couldn’t send'); expect(button('Retry')).toBeUndefined();
		expect(apiFetch.mock.calls.every(([url]) => String(url).endsWith('/discard'))).toBe(true);
	});
	it('saves the last edit before it sends, and sends nothing when that save fails', async () => {
		let saved!: (ok: boolean) => void;
		const flush = vi.fn(() => new Promise<boolean>((r) => { saved = r; }));
		apiFetch.mockResolvedValue(response('sent'));
		render({ flush }); press('Send now'); await settle();
		expect(flush).toHaveBeenCalledTimes(1); expect(apiFetch).not.toHaveBeenCalled(); expect(host.textContent).toContain('Sending…');
		saved(false); await settle();
		expect(apiFetch).not.toHaveBeenCalled(); expect(host.textContent).toContain('Couldn’t save your last edit, so nothing was sent.'); expect(button('Send now')).toBeTruthy();
		press('Send now'); saved(true); await settle();
		expect(apiFetch).toHaveBeenCalledTimes(1); expect(host.textContent?.trim()).toBe('Sent');
	});
	it('locks the editor from the click, and keeps it locked when the send is unconfirmed', async () => {
		let ended = false;
		let answer!: (value: unknown) => void;
		apiFetch.mockReturnValue(new Promise((r) => { answer = r; }));
		component = mount(EmailSendBar, { target: host, props: { artifactId: 'a/1', text: () => 'x', get ended() { return ended; }, set ended(v: boolean) { ended = v; } } }); flushSync();
		expect(ended).toBe(false);
		press('Send now'); await settle(); expect(ended).toBe(true);
		answer(response('unconfirmed')); await settle();
		expect(ended).toBe(true); expect(host.textContent).toContain('If it isn’t there, ask the agent for a new draft.');
	});
	it.each(['changed', 'newer'])('shows the %s refusal and rereads the view', async (state) => {
		const reread = vi.fn(); apiFetch.mockResolvedValue(response(state, 409, 'Review again')); render({ onReread: reread }); press('Send now'); await settle(); expect(host.textContent).toContain('Review again'); expect(button('Send now')).toBeTruthy(); expect(reread).toHaveBeenCalledTimes(1);
	});
	it.each(['sending', 'unconfirmed'])('does not offer another send for %s', async (state) => { apiFetch.mockResolvedValue(response(state)); render(); press('Send now'); await settle(); expect(host.textContent).toContain('Couldn’t confirm the email left.'); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it.each([['sent', 'Sent'], ['discarded', 'Not sent.'], ['sending', 'Couldn’t confirm the email left.']])('opens %s in its settled state', (state, expected) => { render({ initialState: state, readOnly: true }); expect(host.textContent).toContain(expected); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it('blocks offline and unavailable attachments with a reason', () => { render({ connected: false }); expect(host.textContent).toContain('Offline.'); expect(button('Send now')?.getAttribute('aria-disabled')).toBe('true'); press('Send now'); expect(apiFetch).not.toHaveBeenCalled(); });
	it('names a missing attachment', () => { render({ blocker: 'file.pdf' }); expect(host.textContent).toContain('file.pdf is unavailable.'); expect(button('Send now')?.getAttribute('aria-disabled')).toBe('true'); });
	it('shows read-only for an open draft', () => { render({ readOnly: true }); expect(host.textContent).toContain('Read-only. This draft can’t be sent from here.'); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it.each([[401, 'signin'], [404, 'missing']])('maps %s to the existing context refusal', async (status, reason) => { const refused = vi.fn(); apiFetch.mockResolvedValue(response('', status as number)); render({ onRefused: refused }); press('Send now'); await settle(); expect(refused).toHaveBeenCalledWith(reason); });
});
