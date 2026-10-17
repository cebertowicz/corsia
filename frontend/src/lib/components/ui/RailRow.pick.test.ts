import { readFileSync } from 'node:fs';
import { flushSync, mount, unmount } from 'svelte';
import { afterEach, describe, expect, it } from 'vitest';
import RailRow from './RailRow.svelte';

const source = readFileSync('src/lib/components/ui/RailRow.svelte', 'utf8');
let component: ReturnType<typeof mount> | null = null;
afterEach(async () => { if (component) await unmount(component); component = null; document.body.innerHTML = ''; });

/** CSS specificity as [ids, classes and pseudo-classes, tags] for a compound selector like `button.row:hover`. */
function specificity(selector: string): number[] {
	const classes = selector.match(/\.[\w-]+|:(?!:)[\w-]+/g)?.length ?? 0;
	const tags = selector.replace(/\.[\w-]+|:(?!:)[\w-]+/g, ' ').trim().split(/\s+/).filter(Boolean).length;
	return [0, classes, tags];
}
const outranks = (a: number[], b: number[]) => a.findIndex((n, i) => n !== b[i]) >= 0 && a[a.findIndex((n, i) => n !== b[i])] > b[a.findIndex((n, i) => n !== b[i])];
const rule = (selector: string) => new RegExp(`^\\s*${selector.replace(/[.:]/g, '\\$&')}\\s*\\{([^}]*)\\}`, 'm').exec(source)?.[1];

describe('RailRow pick mode', () => {
	it('renders a checked pick row as a checked, clickable checkbox on the current wash', () => {
		component = mount(RailRow, { target: document.body, props: { label: 'Claude', pick: 'many', checked: true, onclick: () => undefined } });
		flushSync();
		const row = document.querySelector('button.row')!;
		expect(row.getAttribute('role')).toBe('checkbox');
		expect(row.getAttribute('aria-checked')).toBe('true');
		expect(row.classList.contains('pick') && row.classList.contains('current')).toBe(true);
	});

	it('keeps a checked pick row on the accent wash under the pointer', () => {
		expect(rule('.row.pick.current:hover')?.trim()).toBe('background: var(--accent-wash);');
		expect(rule('.row.current')).toContain('background: var(--accent-wash)');
		expect(outranks(specificity('.row.pick.current:hover'), specificity('button.row:hover'))).toBe(true);
	});
});
