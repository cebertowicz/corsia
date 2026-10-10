import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';

const setModelImageInput = vi.fn(async (_s: string, _m: string, _mode: string) => ({ ok: true }));
vi.mock('$lib/stores/networkServers', () => ({
	setModelImageInput: (s: string, m: string, mode: string) => setModelImageInput(s, m, mode),
}));

import ImageInputEditor from './ImageInputEditor.svelte';
import type { ImageInput } from '$lib/stores/providerModels';

function button(): HTMLButtonElement {
	return document.querySelector('.actions button') as HTMLButtonElement;
}

describe('ImageInputEditor', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		setModelImageInput.mockClear();
	});

	function open(imageInput: ImageInput | null, onSaved = vi.fn()) {
		component = mount(ImageInputEditor, {
			target: document.body,
			props: { serverId: 'ns-plan', modelId: 'deepseek-v4.1-flash', imageInput, onSaved },
		});
		flushSync();
		return onSaved;
	}

	const status = () => document.querySelector('[data-testid="image-input-status"]')?.textContent ?? '';
	const select = () => document.querySelector('select') as HTMLSelectElement;

	it('says a model not yet checked is not checked, and offers to detect now', () => {
		open(null);
		expect(status()).toContain('Not checked yet');
		expect(select().value).toBe('auto');
		expect(button().textContent?.trim()).toBe('Detect now');
	});

	it('shows what detection found', () => {
		open({ source: 'detected', result: 'yes', detail: 'Named the colour of a test image.', checked_at: 't' });
		expect(status()).toBe('Accepts images. Named the colour of a test image.');
	});

	it('pins a choice through the endpoint and reports back', async () => {
		const onSaved = open({ source: 'detected', result: 'no', detail: 'x', checked_at: 't' });
		select().value = 'yes';
		select().dispatchEvent(new Event('change', { bubbles: true }));
		flushSync();
		button().click();
		await tick();
		await tick();
		expect(setModelImageInput).toHaveBeenCalledWith('ns-plan', 'deepseek-v4.1-flash', 'yes');
		expect(onSaved).toHaveBeenCalled();
	});

	it('shows a user choice as the selected option', () => {
		open({ source: 'user', result: 'no', detail: 'Set by you: Text only.', checked_at: 't' });
		expect(select().value).toBe('no');
		expect(button().disabled).toBe(true);
	});
});
