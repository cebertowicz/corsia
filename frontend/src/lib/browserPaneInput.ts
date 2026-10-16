import { AnimationFrameCoalescer, type PanePoint } from './browserPane';

type MouseInput = { type: 'input_mouse'; eventType: string; x: number; y: number; button?: string; clickCount?: number; deltaX?: number; deltaY?: number };
type WheelDelta = { deltaX: number; deltaY: number; deltaMode: number };

// CDP's mouseWheel deltas are CSS pixels; a line is the 40 px Chrome scrolls per wheel line.
const WHEEL_LINE_PIXELS = 40;

/** A wheel event's deltas in CSS pixels, whatever unit (pixel, line or page) the device reported. */
export function wheelPixels(event: WheelDelta, pageHeight: number): { deltaX: number; deltaY: number } {
	const unit = event.deltaMode === 1 ? WHEEL_LINE_PIXELS : event.deltaMode === 2 ? pageHeight : 1;
	return { deltaX: event.deltaX * unit, deltaY: event.deltaY * unit };
}
type PointerPosition = { pointerId: number; pointerType: string; clientX: number; clientY: number };

/** Mouse positions replace each other; touch wheel deltas preserve every drag movement. */
export class BrowserPaneInput {
	private pressed: number | null = null;
	private touch: { start: PointerPosition; last: PointerPosition; point: PanePoint; dragging: boolean } | null = null;
	private moves = new AnimationFrameCoalescer<PanePoint>(point => this.mouse('mouseMoved', point));

	constructor(private send: (message: MouseInput) => void) {}

	private mouse(eventType: string, point: PanePoint): void {
		this.send({ type: 'input_mouse', eventType, ...point, button: eventType === 'mouseMoved' && this.pressed === null ? 'none' : 'left', clickCount: eventType === 'mouseMoved' ? 0 : 1 });
	}

	press(event: PointerPosition, point: PanePoint): void {
		if (this.pressed !== null || this.touch) return;
		this.moves.cancel();
		if (event.pointerType === 'touch') {
			this.touch = { start: event, last: event, point, dragging: false };
			return;
		}
		this.mouse('mouseMoved', point);
		this.pressed = event.pointerId;
		this.mouse('mousePressed', point);
	}

	move(event: PointerPosition, point: PanePoint, scale: number): void {
		if (event.pointerType !== 'touch') {
			this.moves.push(point);
			return;
		}
		const touch = this.touch;
		if (!touch || event.pointerId !== touch.start.pointerId) return;
		if (Math.hypot(event.clientX - touch.start.clientX, event.clientY - touch.start.clientY) > 8) touch.dragging = true;
		if (!touch.dragging) return;
		const deltaX = (touch.last.clientX - event.clientX) / scale;
		const deltaY = (touch.last.clientY - event.clientY) / scale;
		this.send({ type: 'input_mouse', eventType: 'mouseWheel', ...point, deltaX, deltaY });
		touch.last = event;
	}

	release(event: PointerPosition, point: PanePoint, cancelled = false): void {
		this.moves.cancel();
		const touch = this.touch;
		if (event.pointerType === 'touch') {
			if (!touch || event.pointerId !== touch.start.pointerId) return;
			if (!touch.dragging && !cancelled) {
				this.mouse('mouseMoved', touch.point);
				this.mouse('mousePressed', touch.point);
				this.mouse('mouseReleased', point);
			}
			this.touch = null;
		} else if (this.pressed === event.pointerId) {
			this.mouse('mouseReleased', point);
			this.pressed = null;
		}
	}


	reset(): void {
		this.moves.cancel();
		this.pressed = null;
		this.touch = null;
	}

	dispose(): void {
		this.reset();
	}
}
