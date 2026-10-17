import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ConfirmDialog from './ConfirmDialog.svelte';

let component: Record<string, unknown> | null = null;

function render(props: Record<string, unknown> = {}) {
	const onConfirm = vi.fn();
	const onCancel = vi.fn();
	component = mount(ConfirmDialog, {
		target: document.body,
		props: { open: true, title: 'Delete session', description: 'This cannot be undone.', onConfirm, onCancel, ...props },
	});
	flushSync();
	return { onConfirm, onCancel };
}

function footButtons() {
	return Array.from(document.querySelectorAll<HTMLButtonElement>('[role=alertdialog] button')).filter((b) =>
		['Cancel', 'Confirm', 'Delete'].includes(b.textContent?.trim() ?? ''),
	);
}

afterEach(() => {
	if (component) unmount(component);
	component = null;
	document.body.innerHTML = '';
});

describe('ConfirmDialog', () => {
	it('renders nothing while closed', () => {
		render({ open: false });
		expect(document.querySelector('[role=alertdialog]')).toBeNull();
	});

	it('shows the title and description and no icon tile', () => {
		render();
		const dialog = document.querySelector('[role=alertdialog]')!;
		expect(dialog).not.toBeNull();
		expect(dialog.textContent).toContain('Delete session');
		expect(dialog.textContent).toContain('This cannot be undone.');
		expect(dialog.querySelector('.icon')).toBeNull();
	});

	it('puts Cancel before the confirm action and wires both callbacks', () => {
		const { onConfirm, onCancel } = render({ confirmLabel: 'Delete', destructive: true });
		const [cancel, confirm] = footButtons();
		expect(cancel.textContent?.trim()).toBe('Cancel');
		expect(confirm.textContent?.trim()).toBe('Delete');
		confirm.click();
		expect(onConfirm).toHaveBeenCalledTimes(1);
		cancel.click();
		expect(onCancel).toHaveBeenCalledTimes(1);
	});

	it('uses the danger button only for destructive confirmations', () => {
		render({ confirmLabel: 'Delete', destructive: true });
		expect(footButtons()[1].className).toContain('danger');
		unmount(component!);
		component = null;
		document.body.innerHTML = '';
		render({ confirmLabel: 'Delete' });
		expect(footButtons()[1].className).not.toContain('danger');
		expect(footButtons()[1].className).toContain('primary');
	});
});
