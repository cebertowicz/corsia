import { paneAppearance } from './browserPaneGeometry';

/** Tells the page the pane's size, pixel density and CORSIA theme, so it fills the pane sharply and never glaring. */
export class PaneAppearance {
	private last = '';

	constructor(
		private readonly canvas: () => HTMLCanvasElement | undefined,
		private readonly socket: () => WebSocket | null,
		private readonly scheme: () => string,
	) {}

	/** A new page target knows nothing yet, so the next send goes out whatever was sent before. */
	forget() { this.last = ''; }

	send() {
		const canvas = this.canvas();
		const socket = this.socket();
		if (!canvas || socket?.readyState !== WebSocket.OPEN) return;
		const appearance = paneAppearance(canvas, this.scheme());
		const key = appearance && JSON.stringify(appearance);
		if (!key || key === this.last) return;
		this.last = key;
		socket.send(key);
	}
}
