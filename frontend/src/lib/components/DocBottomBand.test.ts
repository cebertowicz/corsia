import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';
import DocBottomBand from './DocBottomBand.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function render(confirmsDelivery: boolean) {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(DocBottomBand, {
		target: host,
		props: { artifactId: 'artifact-1', threads: [], root: host, ready: true, covered: false, sheetModalOpen: false, onJump: () => undefined, confirmsDelivery },
	});
	deliveryConfirmation.show('artifact-1', 1);
	flushSync();
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	deliveryConfirmation.dismiss();
	host.remove();
});

describe('DocBottomBand', () => {
	it('puts no delivery notice over a document, whose header says it was sent', () => {
		render(false);
		expect(host.querySelector('[data-doc-bottom-band]')).toBeNull();
		expect(host.querySelector('.delivery-confirmation')).toBeNull();
	});

	it('shows the delivery notice in an image viewer\'s band', () => {
		render(true);
		expect(host.querySelector('[data-doc-bottom-band] .delivery-confirmation')?.textContent).toContain('1 comment sent');
	});
});
