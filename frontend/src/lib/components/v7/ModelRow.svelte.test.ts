import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ModelRow from './ModelRow.svelte';

describe('ModelRow warning line', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
	});

	it('shows the warning under the row', () => {
		component = mount(ModelRow, {
			target: document.body,
			props: { name: 'glm-5.3', toggleOn: true, warning: 'Not shown in the model list: set its context window in this model’s details.' },
		});
		flushSync();
		const warn = document.querySelector('[role="status"]');
		expect(warn?.textContent).toBe('Not shown in the model list: set its context window in this model’s details.');
		expect(document.querySelector('.mr')?.classList.contains('has-warn')).toBe(true);
	});

	it('renders no warning line when there is nothing to say', () => {
		component = mount(ModelRow, { target: document.body, props: { name: 'glm-5.3', toggleOn: true } });
		flushSync();
		expect(document.querySelector('[role="status"]')).toBeNull();
		expect(document.querySelector('.mr')?.classList.contains('has-warn')).toBe(false);
	});
});

describe('ModelRow switch label', () => {
	afterEach(() => { document.body.innerHTML = ''; });

	const label = (props: Record<string, unknown>) => {
		const c = mount(ModelRow, { target: document.body, props: { name: 'm', ...props } });
		flushSync();
		const text = document.querySelector('button.tg')?.getAttribute('aria-label');
		unmount(c);
		return text;
	};

	it('says load and unload for a local engine by default', () => {
		expect(label({ toggleOn: true })).toBe('Unload');
		expect(label({ toggleOn: false })).toBe('Load');
	});

	it('names what the switch does when the caller says so', () => {
		const toggleLabels = ['Hide from picker', 'Show in picker'];
		expect(label({ toggleOn: true, toggleLabels })).toBe('Hide from picker');
		expect(label({ toggleOn: false, toggleLabels })).toBe('Show in picker');
	});

	it('offers Delete download from the row menu without removing the load control', () => {
		const onDeleteDownload = vi.fn();
		const component = mount(ModelRow, { target: document.body, props: { name: 'Qwen', toggleOn: false, onDeleteDownload } });
		flushSync();
		const row = document.querySelector<HTMLElement>('.mr')!;
		row.focus();
		row.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ContextMenu' }));
		flushSync();
		expect(document.body.textContent).toContain('Delete download');
		expect(document.querySelector<HTMLButtonElement>('button.tg')?.getAttribute('aria-label')).toBe('Load');
		[...document.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Delete download')!.click();
		expect(onDeleteDownload).toHaveBeenCalledOnce();
		unmount(component);
	});

	it('shows no control at all on a download in flight, only the loading puck elsewhere', () => {
		const loading = mount(ModelRow, { target: document.body, props: { name: 'Qwen', state: 'loading', progress: 40 } });
		flushSync();
		expect(document.querySelector('.puck')).not.toBeNull();
		unmount(loading);
		const download = mount(ModelRow, { target: document.body, props: { name: 'Qwen', state: 'loading', progress: 40, puck: false } });
		flushSync();
		expect(document.querySelector('.puck')).toBeNull();
		expect(document.querySelector('button.tg')).toBeNull();
		expect(document.querySelector('.mr-toggle-wrap')).not.toBeNull();
		unmount(download);
	});
});
