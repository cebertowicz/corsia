import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
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
			props: { name: 'glm-5.3', toggleOn: true, warning: 'Not in the chat picker: set a window.' },
		});
		flushSync();
		const warn = document.querySelector('[role="status"]');
		expect(warn?.textContent).toBe('Not in the chat picker: set a window.');
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
});
