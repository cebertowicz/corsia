import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import DeliveryConfirmation from './DeliveryConfirmation.svelte';
import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	deliveryConfirmation.dismiss();
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DeliveryConfirmation, { target: host, props: { artifactId: 'artifact-1' } });
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	deliveryConfirmation.dismiss();
	host.remove();
});

describe('DeliveryConfirmation', () => {
	it('uses the shared toast element in its artifact canvas and dismisses only that confirmation', () => {
		deliveryConfirmation.show('artifact-1', 2);
		flushSync();

		const notice = host.querySelector<HTMLElement>('.toast.toast--success')!;
		expect(notice).toBeTruthy();
		expect(notice.getAttribute('role')).toBe('status');
		expect(notice.textContent).toContain('2 comments sent');
		host.querySelector<HTMLButtonElement>('.toast-close')!.click();
		expect(get(deliveryConfirmation)).toBeNull();
	});

	it('renders only in the canvas for the delivered artifact', () => {
		deliveryConfirmation.show('another-artifact', 1);
		flushSync();

		expect(host.querySelector('.delivery-confirmation')).toBeNull();
	});
});
