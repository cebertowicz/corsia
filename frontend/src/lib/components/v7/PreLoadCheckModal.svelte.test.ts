import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import PreLoadCheckModal from './PreLoadCheckModal.svelte';

let component: ReturnType<typeof mount> | null = null;

afterEach(async () => {
	if (component) await unmount(component);
	component = null;
	document.body.innerHTML = '';
});

function render(props: { estimatedGb: number; freeGb: number; afterLoadGb: number; safeReserveGb: number; warning: string }) {
	component = mount(PreLoadCheckModal, { target: document.body, props: { modelName: 'Big model', onConfirm: vi.fn(), onCancel: vi.fn(), ...props } });
	flushSync();
	return document.body.textContent ?? '';
}

describe('PreLoadCheckModal', () => {
	it('is "Tight on memory" while the load still fits, with the real safe reserve', () => {
		const text = render({ estimatedGb: 20, freeGb: 30, afterLoadGb: 10, safeReserveGb: 16, warning: 'Loading would leave 10.0 GB of memory free (below the 16 GB safe reserve).' });
		expect(text).toContain('Tight on memory');
		expect(text).toContain('16 GB safe reserve');
		expect(text).toContain('close to the limit');
	});

	it('is "Not enough memory" when the load exceeds what is free, so the title agrees with the warning', () => {
		const text = render({ estimatedGb: 174, freeGb: 123, afterLoadGb: -51, safeReserveGb: 16, warning: 'Loading would need 51.0 GB more memory than is free.' });
		expect(text).toContain('Not enough memory');
		expect(text).not.toContain('Tight on memory');
		expect(text).toContain('need more memory than is free');
		expect(text).toContain('51.0 GB more memory');
	});

	it('says the swap-or-kill consequence once, in memory terms', () => {
		const text = render({ estimatedGb: 174, freeGb: 123, afterLoadGb: -51, safeReserveGb: 16, warning: 'Loading would need 51.0 GB more memory than is free.' }).replace(/\s+/g, ' ');
		expect(text.match(/swap or kill/g)).toHaveLength(1);
		expect(text).toContain('Unload other models to free memory');
	});
});
