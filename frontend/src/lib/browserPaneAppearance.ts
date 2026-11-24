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

	/**
	 * The save offer's dock makes the canvas shorter. While it is open the page keeps the height it had before;
	 * density and theme still go through. A change of width (a resized window or side panel, a rotated phone)
	 * ends the hold, so the page takes the pane's real size as it would without the dock.
	 */
	holdSize(held: boolean) {
		const canvas = this.canvas();
		this.sizeBeforeOffer = held && canvas ? paneSize(canvas) : undefined;
		// The canvas is back to its full size by the time the hold ends, so no resize follows to say so.
		if (!held) this.send();
	}

	/** A new page target knows nothing yet, so the next send goes out whatever was sent before. */
	forget() { this.last = ''; }

	send() {
		const canvas = this.canvas();
		const socket = this.socket();
		if (!canvas || socket?.readyState !== WebSocket.OPEN) return;
		if (this.sizeBeforeOffer && this.sizeBeforeOffer.width !== paneSize(canvas).width) this.sizeBeforeOffer = undefined;
		const appearance = paneAppearance(canvas, this.scheme(), this.sizeBeforeOffer);
		const key = appearance && JSON.stringify(appearance);
		if (!key || key === this.last) return;
		this.last = key;
		socket.send(key);
	}
}
