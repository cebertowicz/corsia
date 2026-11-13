import { afterEach, describe, expect, it } from 'vitest';
import { deleteCharBackward, deleteCharForward } from '@codemirror/commands';
import { imageContext } from './markdown-image';
import type { Rig } from './typed-reveal-rig';
import { build, caretAt, cellEditor, cellText, cleanup, clicked } from './widget-cell-rig';

/**
 * A cell holding a workspace image is drawn with the image; opened, it still shows the image, as the document does, and
 * its markdown is one unit: deleting next to it removes the image whole or not at all, never part of its markdown.
 */

const CONTEXT = { docPath: '/ws/notes/plan.md', projectId: 'p1', token: 't' };
const IMAGE = '![](img/a.png)';
const docWith = (cell: string) => `# Shots\n\n| Shot | Note |\n| --- | --- |\n| ${cell} | x |\n\nafter\n`;
const rowCell = (r: Rig) => /^\| (.*?) \| x \|$/m.exec(r.view.state.doc.toString().split('\n')[4])?.[1] ?? null;

const rigs: Rig[] = [];
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; });

const openImageCell = (cell: string) => {
	const r = build(docWith(cell), [imageContext.of(() => CONTEXT)]);
	rigs.push(r);
	const td = r.view.dom.querySelector('table')!.rows[1].cells[0];
	expect(td.querySelector('img'), 'setup: the drawn cell shows the image').not.toBeNull();
	clicked(r, td);
	expect(cellEditor(r), 'setup: the cell opened').not.toBeNull();
	return r;
};

describe('an image in an open cell', () => {
	for (const cell of [IMAGE, `see ${IMAGE} here`]) {
		it(`still shows the image, and deleting next to it never leaves part of its markdown (${cell})`, () => {
			const r = openImageCell(cell);
			const img = cellEditor(r)!.dom.querySelector<HTMLImageElement>('.cm-md-image img');
			expect(img, 'the open cell shows the image').not.toBeNull();
			expect(new URL(img!.src, 'http://x').searchParams.get('path')).toBe('/ws/notes/img/a.png');
			const start = cell.indexOf(IMAGE), end = start + IMAGE.length;
			const whole = new RegExp(`^${cell.slice(0, start)}(${IMAGE.replace(/[![\]().]/g, '\\$&')})?${cell.slice(end)}$`.replace(/ +/g, ' *'));
			for (const [at, command] of [[end, deleteCharBackward], [start, deleteCharForward]] as const) {
				caretAt(r, at);
				command(cellEditor(r)!);
				expect(cellText(r), `${command.name} at ${at} removes the image whole or not at all`).toMatch(whole);
				expect(rowCell(r) ?? '', 'and the row holds the same').toMatch(whole);
				if (!cellText(r).includes(IMAGE)) return;
			}
		});
	}
});
