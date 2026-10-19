import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import CorsiaMessageRequestHandler from './CorsiaMessageRequestHandler.svelte';
import { pendingCorsiaMessage, requestCorsiaMessage } from '$lib/corsiaRequest';
import { toasts } from '$lib/stores/toast';
import { uiScope } from '$lib/stores/uiScope';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	pendingCorsiaMessage.set(null);
	uiScope.set(null);
	if (component) unmount(component);
	component = null;
	host?.remove();
	vi.restoreAllMocks();
});

describe('CorsiaMessageRequestHandler', () => {
	it('shows an error and clears a request that cannot open its CORSIA conversation', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		const error = vi.spyOn(console, 'error').mockImplementation(() => {});
		const toast = vi.spyOn(toasts, 'show');
		const send = vi.fn();
		component = mount(CorsiaMessageRequestHandler, {
			target: host,
			props: {
				goConductor: vi.fn(async () => false),
				chatPanel: { setAndSend: send } as never,
			},
		});
		flushSync();

		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());

		expect(send).not.toHaveBeenCalled();
		expect(error).toHaveBeenCalledWith('CORSIA request was not sent because its conversation could not open');
		expect(toast).toHaveBeenCalledWith("Couldn't open CORSIA's chat. Try again.", 'error');
	});

	async function deliverWith(outcome: 'sent' | 'not_sent' | 'notified') {
		host = document.createElement('div');
		document.body.appendChild(host);
		const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
		const toast = vi.spyOn(toasts, 'show');
		const send = vi.fn(async () => outcome);
		component = mount(CorsiaMessageRequestHandler, {
			target: host,
			props: {
				goConductor: vi.fn(async () => true),
				chatPanel: { setAndSend: send } as never,
			},
		});
		flushSync();
		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());
		expect(send).toHaveBeenCalledWith('Connect a Google account for all my projects', true);
		return { warn, toast };
	}

	it('opens and sends to workspace CORSIA while a project scope is active', async () => {
		host = document.createElement('div');
		document.body.appendChild(host);
		uiScope.set({ id: 'project-1', name: 'Project', color: '#000' });
		const openWorkspaceCorsia = vi.fn(async () => true);
		const send = vi.fn(async () => 'sent');
		component = mount(CorsiaMessageRequestHandler, {
			target: host,
			props: { goConductor: openWorkspaceCorsia, chatPanel: { setAndSend: send } as never },
		});
		flushSync();

		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());

		expect(openWorkspaceCorsia).toHaveBeenCalledWith(expect.any(Function), null);
		expect(send).toHaveBeenCalledWith('Connect a Google account for all my projects', true);
	});

	it('says once that the request was not sent when the composer refused it silently', async () => {
		const { warn, toast } = await deliverWith('not_sent');

		expect(toast).toHaveBeenCalledOnce();
		expect(toast).toHaveBeenCalledWith("Couldn't send that to CORSIA. Try again.", 'error');
		expect(warn).toHaveBeenCalledWith("CORSIA request was not sent; the composer kept the person's own draft");
	});

	it('adds no second toast when the composer already showed its connection-lost toast', async () => {
		const { warn, toast } = await deliverWith('notified');

		expect(toast).not.toHaveBeenCalled();
		expect(warn).toHaveBeenCalledOnce();
	});

	it('stays quiet when the request was sent', async () => {
		const { warn, toast } = await deliverWith('sent');

		expect(toast).not.toHaveBeenCalled();
		expect(warn).not.toHaveBeenCalled();
	});

	it.each(['goConductor', 'setAndSend'] as const)('clears the request and shows the not-sent toast when %s rejects', async (rejecting) => {
		host = document.createElement('div');
		document.body.appendChild(host);
		const toast = vi.spyOn(toasts, 'show');
		const error = vi.spyOn(console, 'error').mockImplementation(() => {});
		const send = vi.fn(async () => rejecting === 'setAndSend' ? Promise.reject(new Error('send failed')) : 'sent');
		component = mount(CorsiaMessageRequestHandler, {
			target: host,
			props: {
				goConductor: vi.fn(async () => rejecting === 'goConductor' ? Promise.reject(new Error('open failed')) : true),
				chatPanel: { setAndSend: send } as never,
			},
		});
		flushSync();

		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());

		expect(toast).toHaveBeenCalledWith("Couldn't send that to CORSIA. Try again.", 'error');
		expect(error).toHaveBeenCalledWith('CORSIA request was not sent because delivery failed', expect.any(Error));
	});
});
