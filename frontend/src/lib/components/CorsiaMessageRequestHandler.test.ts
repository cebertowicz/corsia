import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import CorsiaMessageRequestHandler from './CorsiaMessageRequestHandler.svelte';
import { pendingCorsiaMessage, requestCorsiaMessage } from '$lib/corsiaRequest';
import { toasts } from '$lib/stores/toast';

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

afterEach(() => {
	pendingCorsiaMessage.set(null);
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
				active: () => true,
			},
		});
		flushSync();

		requestCorsiaMessage('Connect a Google account for all my projects');
		await vi.waitFor(() => expect(get(pendingCorsiaMessage)).toBeNull());

		expect(send).not.toHaveBeenCalled();
		expect(error).toHaveBeenCalledWith('CORSIA request was not sent because its conversation could not open');
		expect(toast).toHaveBeenCalledWith("Couldn't open CORSIA's chat. Try again.", 'error');
	});
});
