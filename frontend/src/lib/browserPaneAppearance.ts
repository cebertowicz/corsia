import { paneAppearance, paneSize, type PaneSize } from './browserPaneGeometry';

/** Tells the page the pane's size, pixel density and CORSIA theme, so it fills the pane sharply and never glaring. */
export class PaneAppearance {
	private last = '';
	private sizeBeforeOffer: PaneSize | undefined;

	constructor(
		private readonly canvas: () => HTMLCanvasElement | undefined,
		private readonly socket: () => WebSocket | null,
		private readonly scheme: () => string,
	) {}

	/** The save offer's dock makes the canvas shorter. While it is open the page keeps the size it had before; density and theme still go through. */
	holdSize(held: boolean) {
		const canvas = this.canvas();
		this.sizeBeforeOffer = held && canvas ? paneSize(canvas) : undefined;
	}

	/** A new page target knows nothing yet, so the next send goes out whatever was sent before. */
	forget() { this.last = ''; }

	send() {
		const canvas = this.canvas();
		const socket = this.socket();
		if (!canvas || socket?.readyState !== WebSocket.OPEN) return;
		const appearance = paneAppearance(canvas, this.scheme(), this.sizeBeforeOffer);
		const key = appearance && JSON.stringify(appearance);
		if (!key || key === this.last) return;
		this.last = key;
		socket.send(key);
	}
}
