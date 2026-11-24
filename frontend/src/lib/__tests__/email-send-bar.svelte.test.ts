import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import EmailSendBar from '$lib/components/EmailSendBar.svelte';
import { fetchEmailDraftContext } from '$lib/api/emailDrafts';

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock('$lib/api/request', () => ({ apiFetch }));
let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const response = (state: string, status = 200, message = '') => ({ ok: status === 200, status, json: async () => status === 409 ? { detail: { state, message } } : { state } });
const button = (name: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name);
const press = (name: string) => { button(name)!.click(); flushSync(); };
const render = (props: Record<string, unknown> = {}) => { component = mount(EmailSendBar, { target: host, props: { artifactId: 'a/1', text: () => 'To: a@example.com\n\nExact ✉\n', readAt: 'read-time', ...props } }); flushSync(); };
const settle = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); flushSync(); };
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); apiFetch.mockReset(); });
afterEach(() => { if (component) unmount(component); component = null; host.remove(); });

describe('email send bar', () => {
	it('retains the context read time and state from the server', async () => {
		apiFetch.mockResolvedValue({ ok: true, json: async () => ({ status: 'ready', state: 'open', read_at: 'server-time', conversation: [], attachments: [], notes: [] }) });
		expect(await fetchEmailDraftContext('a')).toMatchObject({ state: 'open', readAt: 'server-time' });
	});
	it('posts the exact editor text and read time once on a double click, then shows Sent', async () => {
		let resolve!: (value: unknown) => void;
		apiFetch.mockReturnValue(new Promise((r) => { resolve = r; }));
		render(); const send = button('Send')!; send.click(); send.click(); flushSync();
		expect(apiFetch).toHaveBeenCalledTimes(1);
		expect(apiFetch).toHaveBeenCalledWith('/api/email-drafts/a%2F1/send', expect.objectContaining({ method: 'POST', body: JSON.stringify({ text: 'To: a@example.com\n\nExact ✉\n', read_at: 'read-time' }) }));
		expect(host.textContent).toContain('Sending…'); expect(host.querySelectorAll('button')).toHaveLength(0);
		resolve(response('sent')); await settle(); expect(host.textContent?.trim()).toBe('Sent');
	});
	it('names addresses the mail host did not deliver to after sending', async () => {
		apiFetch.mockResolvedValue({ ok: true, status: 200, json: async () => ({ state: 'sent', not_delivered_to: ['lee@example.com', 'sam@example.com'] }) });
		render(); press('Send'); await settle(); expect(host.textContent?.trim()).toBe('Sent. lee@example.com, sam@example.com didn’t get it.'); expect(host.querySelectorAll('button')).toHaveLength(0);
	});
	it('confirms discard, keeps a draft on cancel, and sends the discard', async () => {
		apiFetch.mockResolvedValue(response('discarded')); render(); press('Don’t send'); expect(host.textContent).toContain('Don’t send this email?'); press('Keep draft'); press('Don’t send'); press('Don’t send'); await settle();
		expect(apiFetch).toHaveBeenCalledWith('/api/email-drafts/a%2F1/discard', expect.objectContaining({ method: 'POST' })); expect(host.textContent).toContain('Not sent. This draft is marked as not to send.');
	});
	it('offers retry after failure and succeeds', async () => {
		apiFetch.mockResolvedValueOnce(response('failed')).mockResolvedValueOnce(response('sent')); render(); press('Send'); await settle(); expect(host.textContent).toContain('Couldn’t send. The email didn’t leave, and the draft is kept.'); press('Retry'); await settle(); expect(host.textContent?.trim()).toBe('Sent');
	});
	it('handles a network failure', async () => { apiFetch.mockRejectedValue(new Error('offline')); render(); press('Send'); await settle(); expect(button('Retry')).toBeTruthy(); });
	it.each(['changed', 'newer'])('shows the %s refusal and rereads only newer', async (state) => {
		const reread = vi.fn(); apiFetch.mockResolvedValue(response(state, 409, 'Review again')); render({ onReread: reread }); press('Send'); await settle(); expect(host.textContent).toContain('Review again'); expect(button('Send')).toBeTruthy(); expect(reread).toHaveBeenCalledTimes(state === 'newer' ? 1 : 0);
	});
	it.each(['sending', 'unconfirmed'])('does not offer another send for %s', async (state) => { apiFetch.mockResolvedValue(response(state)); render(); press('Send'); await settle(); expect(host.textContent).toContain('Couldn’t confirm the email left.'); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it.each([['sent', 'Sent'], ['discarded', 'Not sent.'], ['sending', 'Couldn’t confirm the email left.']])('opens %s in its settled state', (state, expected) => { render({ initialState: state, readOnly: true }); expect(host.textContent).toContain(expected); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it('blocks offline and unavailable attachments with a reason', () => { render({ connected: false }); expect(host.textContent).toContain('Offline.'); expect(button('Send')?.getAttribute('aria-disabled')).toBe('true'); press('Send'); expect(apiFetch).not.toHaveBeenCalled(); });
	it('names a missing attachment', () => { render({ blocker: 'file.pdf' }); expect(host.textContent).toContain('file.pdf is unavailable.'); expect(button('Send')?.getAttribute('aria-disabled')).toBe('true'); });
	it('shows read-only for an open draft', () => { render({ readOnly: true }); expect(host.textContent).toContain('Read-only. This draft can’t be sent from here.'); expect(host.querySelectorAll('button')).toHaveLength(0); });
	it.each([[401, 'signin'], [404, 'missing']])('maps %s to the existing context refusal', async (status, reason) => { const refused = vi.fn(); apiFetch.mockResolvedValue(response('', status as number)); render({ onRefused: refused }); press('Send'); await settle(); expect(refused).toHaveBeenCalledWith(reason); });
});
