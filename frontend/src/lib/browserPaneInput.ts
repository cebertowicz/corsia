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
// Chrome's CDP modifier bits preserve shortcuts that the person performs in the shared page.
export function keyModifiers(event: KeyboardEvent) {
	return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
}

// Chrome types only keys that carry text; Enter must carry a return to submit a form.
export function keyText(event: KeyboardEvent) {
	if (event.type !== 'keydown' || event.metaKey || event.ctrlKey) return '';
	return event.key === 'Enter' ? '\r' : event.key.length === 1 ? event.key : '';
}

type PointerPosition = { pointerId: number; pointerType: string; clientX: number; clientY: number };

/** Mouse positions replace each other; touch wheel deltas preserve every drag movement. */
export class BrowserPaneInput {
	private pressed: number | null = null;
	private touch: { start: PointerPosition; last: PointerPosition; point: PanePoint; dragging: boolean } | null = null;
	private continuous = new AnimationFrameCoalescer<MouseInput>(message => {
		if (message.eventType === 'mouseWheel') this.send(message);
		else this.mouse('mouseMoved', message);
	}, (previous, next) => previous.eventType === 'mouseWheel' || next.eventType === 'mouseWheel' ? {
		...next, eventType: 'mouseWheel', deltaX: (previous.deltaX ?? 0) + (next.deltaX ?? 0), deltaY: (previous.deltaY ?? 0) + (next.deltaY ?? 0)
	} : next);

	constructor(private send: (message: MouseInput) => void, private readonly onScroll: (delta: PanePoint) => void = () => {}) {}

	private mouse(eventType: string, point: PanePoint): void {
		this.send({ type: 'input_mouse', eventType, ...point, button: eventType === 'mouseMoved' && this.pressed === null ? 'none' : 'left', clickCount: eventType === 'mouseMoved' ? 0 : 1 });
	}

	press(event: PointerPosition, point: PanePoint): void {
		if (this.pressed !== null || this.touch) return;
		this.flush();
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
			this.continuous.push({ type: 'input_mouse', eventType: 'mouseMoved', ...point });
			return;
		}
		const touch = this.touch;
		if (!touch || event.pointerId !== touch.start.pointerId) return;
		if (Math.hypot(event.clientX - touch.start.clientX, event.clientY - touch.start.clientY) > 8) touch.dragging = true;
		if (!touch.dragging) return;
		const deltaX = (touch.last.clientX - event.clientX) / scale;
		const deltaY = (touch.last.clientY - event.clientY) / scale;
		this.wheel(point, { deltaX, deltaY });
		touch.last = event;
	}

	wheel(point: PanePoint, delta: { deltaX: number; deltaY: number }): void {
		// Queue the wheel before predicting it, so a failed redraw cannot lose the input.
		this.continuous.push({ type: 'input_mouse', eventType: 'mouseWheel', ...point, ...delta });
		this.onScroll({ x: delta.deltaX, y: delta.deltaY });
	}

	release(event: PointerPosition, point: PanePoint, cancelled = false): void {
		this.flush();
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
		this.continuous.cancel();
		this.pressed = null;
		this.touch = null;
	}

	flush(): void {
		this.continuous.flush();
	}

	dispose(): void {
		this.reset();
	}
}
