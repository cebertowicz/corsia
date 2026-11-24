import { describe, expect, it } from 'vitest';
import { PaneAppearance } from './browserPaneAppearance';

function pane() {
	const size = { width: 800, height: 600 };
	const canvas = { getBoundingClientRect: () => ({ ...size }) } as unknown as HTMLCanvasElement;
	const sent: { width: number; height: number }[] = [];
	const socket = { readyState: WebSocket.OPEN, send: (raw: string) => sent.push(JSON.parse(raw)) } as unknown as WebSocket;
	const appearance = new PaneAppearance(() => canvas, () => socket, () => 'light');
	return { size, sent, appearance };
}

describe('pane appearance while the save offer is docked', () => {
	it('keeps the page at its pre-dock height while the dock makes the canvas shorter', () => {
		const { size, sent, appearance } = pane();
		appearance.send();
		appearance.holdSize(true);
		size.height = 520;
		appearance.send();
		expect(sent.map(({ width, height }) => [width, height])).toEqual([[800, 600]]);
		appearance.holdSize(false);
		expect(sent.at(-1)).toMatchObject({ width: 800, height: 520 });
	});

	it('gives the page the pane real size once the width changes, as it would without the dock', () => {
		const { size, sent, appearance } = pane();
		appearance.holdSize(true);
		size.height = 520;
		size.width = 400;
		appearance.send();
		expect(sent.at(-1)).toMatchObject({ width: 400, height: 520 });
		// The hold is over: a later return to the old width doesn't bring back the old height.
		size.width = 800;
		appearance.send();
		expect(sent.at(-1)).toMatchObject({ width: 800, height: 520 });
	});
});
