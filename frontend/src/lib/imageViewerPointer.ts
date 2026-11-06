import { distance, fractionOnImage, midpoint, wheelFactor, type Point } from './imageZoom';

/** Wheel and WebKit gesture events must be able to cancel the page's own zoom, so they cannot be passive. */
export function attachImageWheel(el: HTMLDivElement, options: {
	releaseReveal: () => void; loaded: () => boolean; scale: () => number;
	image: () => HTMLImageElement | undefined; zoomTo: (scale: number, pivot: Point) => void;
	placeScale: (scale: number, at: Point, pivot: Point) => void;
}): () => void {
	const onWheel = (event: WheelEvent) => {
		options.releaseReveal();
		if (!event.ctrlKey && !event.metaKey) return;
		event.preventDefault();
		if (options.loaded()) options.zoomTo(options.scale() * wheelFactor(event.deltaY, event.deltaMode), { x: event.clientX, y: event.clientY });
	};
	let gesture: { scale: number; at: Point } | null = null;
	type GestureEvent = Event & { scale: number; clientX: number; clientY: number };
	const gestureStart = (event: Event) => {
		event.preventDefault();
		const e = event as GestureEvent, image = options.image();
		if (image) gesture = { scale: options.scale(), at: fractionOnImage({ x: e.clientX, y: e.clientY }, image.getBoundingClientRect()) };
	};
	const gestureChange = (event: Event) => {
		event.preventDefault();
		const e = event as GestureEvent;
		if (gesture && options.loaded()) options.placeScale(gesture.scale * e.scale, gesture.at, { x: e.clientX, y: e.clientY });
	};
	const gestureEnd = (event: Event) => { event.preventDefault(); gesture = null; };
	el.addEventListener('wheel', onWheel, { passive: false });
	el.addEventListener('gesturestart', gestureStart);
	el.addEventListener('gesturechange', gestureChange);
	el.addEventListener('gestureend', gestureEnd);
	return () => {
		el.removeEventListener('wheel', onWheel);
		el.removeEventListener('gesturestart', gestureStart);
		el.removeEventListener('gesturechange', gestureChange);
		el.removeEventListener('gestureend', gestureEnd);
	};
}

const FLING_FRICTION = 0.94;

/** Touch has no scroll of its own here (touch-action is none, so one finger and two are ours to read). */
export function imageViewerPointer(options: {
	viewport: () => HTMLDivElement | undefined;
	image: () => HTMLImageElement | undefined;
	loaded: () => boolean;
	overflowing: () => boolean;
	reducedMotion: () => boolean;
	scale: () => number;
	releaseReveal: () => void;
	placeScale: (scale: number, at: Point, pivot: Point) => void;
	setPanning: (panning: boolean) => void;
}) {
	const pointers = new Map<number, Point>();
	let pinch: { distance: number; scale: number; at: Point } | null = null;
	let drag: { last: Point; velocity: Point; time: number } | null = null;
	let fling = 0;
	const stopFling = () => { cancelAnimationFrame(fling); fling = 0; };
	function startFling(velocity: Point) {
		const el = options.viewport();
		if (!el || options.reducedMotion() || Math.hypot(velocity.x, velocity.y) < 0.3) return;
		let v = velocity;
		let previous = performance.now();
		const step = (now: number) => {
			const dt = Math.min(48, now - previous);
			previous = now;
			el.scrollLeft -= v.x * dt;
			el.scrollTop -= v.y * dt;
			v = { x: v.x * FLING_FRICTION ** (dt / 16), y: v.y * FLING_FRICTION ** (dt / 16) };
			fling = Math.hypot(v.x, v.y) > 0.02 ? requestAnimationFrame(step) : 0;
		};
		fling = requestAnimationFrame(step);
	}
	function beginPinch() {
		const [a, b] = [...pointers.values()];
		const image = options.image();
		if (!image) return;
		const mid = midpoint(a, b);
		pinch = { distance: Math.max(1, distance(a, b)), scale: options.scale(), at: fractionOnImage(mid, image.getBoundingClientRect()) };
		drag = null;
	}
	function down(event: PointerEvent) {
		options.releaseReveal();
		if (!options.loaded() || (event.pointerType === 'mouse' && (event.button !== 0 || !options.overflowing()))) return;
		stopFling();
		options.viewport()?.setPointerCapture(event.pointerId);
		pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		if (pointers.size === 2) beginPinch();
		else if (pointers.size === 1) {
			drag = { last: { x: event.clientX, y: event.clientY }, velocity: { x: 0, y: 0 }, time: event.timeStamp };
			options.setPanning(event.pointerType === 'mouse');
		}
	}
	function move(event: PointerEvent) {
		const el = options.viewport();
		if (!el || !pointers.has(event.pointerId)) return;
		pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
		if (pinch && pointers.size >= 2) {
			const [a, b] = [...pointers.values()];
			options.placeScale(pinch.scale * (distance(a, b) / pinch.distance), pinch.at, midpoint(a, b));
		} else if (drag) {
			const here = { x: event.clientX, y: event.clientY };
			const dx = here.x - drag.last.x;
			const dy = here.y - drag.last.y;
			el.scrollLeft -= dx;
			el.scrollTop -= dy;
			const dt = Math.max(1, event.timeStamp - drag.time);
			drag = { last: here, velocity: { x: dx / dt, y: dy / dt }, time: event.timeStamp };
		}
	}
	function end(event: PointerEvent) {
		if (!pointers.delete(event.pointerId)) return;
		options.setPanning(false);
		if (pointers.size === 0) {
			const flung = drag && event.pointerType === 'touch' && event.type === 'pointerup' ? drag.velocity : null;
			pinch = null;
			drag = null;
			if (flung) startFling(flung);
		} else if (pointers.size === 1) {
			// One finger of a pinch lifts: the other carries on as a pan from where it is.
			const [rest] = [...pointers.values()];
			pinch = null;
			drag = { last: rest, velocity: { x: 0, y: 0 }, time: event.timeStamp };
		}
	}
	return { down, move, end };
}
