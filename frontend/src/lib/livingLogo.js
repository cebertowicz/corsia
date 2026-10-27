// The living CORSIA mark: the 38 circles of corsia-mark.svg, each an element of its own.
// Framework-free: the app's Svelte components and the launch-shell page both import this file, so tuning the motion is one change.
//
// What moves (every number is a constant below):
//   entry   each dot lands on its own, in a ripple spreading out from one centre; it overshoots slightly and settles. Exit is the inverse.
//   flow    once the entry has settled, the logo's own colours slide down the C and wrap round, forever, very slowly.
//   pulse   one dot at a time swells (its own geometry), 3.2 to 5 s apart, never two at once.
//   sheen   optional. Every dot gets a faint 3D highlight, and the pulsing dot steps into a light that slides across it.
// Entry, exit and pulse move transforms and opacity only; the flow animates the dots' fill with no script running per frame.
// With prefers-reduced-motion the mark is built and stays still.

/** @typedef {[cx: number, cy: number, r: number, fill: string]} Dot */

/** The mark's circles, in the order of the logo's gradient (orange at the top of the C, blue at the bottom). */
/** @type {Dot[]} */
export const DOTS = [[650.6,91.7,27.7,'#FD7E27'],[560.5,107.1,26.0,'#FC7C2A'],[478.0,138.8,27.2,'#FB7A2D'],[401.4,189.3,27.4,'#F9723A'],[764.6,198.7,33.3,'#F8703D'],[659.4,198.9,36.0,'#F86E41'],[555.0,228.0,36.4,'#F5674C'],[336.9,252.1,27.7,'#F46353'],[463.4,287.3,35.7,'#F05668'],[708.7,312.8,31.3,'#E531A3'],[622.9,327.5,31.4,'#E430A5'],[289.2,327.8,27.8,'#EC497E'],[404.8,366.5,35.1,'#E73899'],[551.9,370.5,30.7,'#E32EAA'],[265.6,414.9,29.8,'#CC29C3'],[506.5,434.9,30.2,'#E229B2'],[374.3,456.4,34.5,'#C52BC8'],[492.8,507.9,30.1,'#DA27BA'],[257.6,512.8,31.5,'#9432E6'],[699.7,520.6,63.0,'#E126B6'],[373.5,550.0,34.5,'#9B31E1'],[504.7,580.4,29.3,'#A530DB'],[265.7,605.5,29.1,'#673DFC'],[399.3,640.1,34.8,'#7737F8'],[545.3,643.4,29.2,'#8535EF'],[610.9,688.1,29.5,'#7437F9'],[293.9,690.9,28.5,'#484EFC'],[695.8,706.5,30.0,'#6D3AFC'],[451.7,719.3,35.8,'#5A45FC'],[339.0,769.4,28.6,'#176AFC'],[538.6,781.5,35.9,'#3957FC'],[639.5,815.4,35.0,'#2165FC'],[746.2,818.1,34.8,'#186AFC'],[399.9,833.7,29.1,'#106EFC'],[479.4,886.6,29.5,'#0D6FFC'],[563.3,918.5,29.0,'#0C70FC'],[741.6,924.7,29.3,'#0C70FC'],[651.4,930.8,29.2,'#0C70FC']];

/** Where the entry ripple starts and the exit ripple ends: the big dot at the C's open end, or the C's visual centre (the mean of the dots). */
export const ORIGINS = { dot: [699.7, 520.6], centre: [507.5, 519.7] };

/** The two looks the prototype let the owner compare, each fixed here as one constant: the faint 3D sheen (false is flat), and where the ripple starts (the C's centre, or the big dot). */
export const SHEEN = true;
/** @type {keyof typeof ORIGINS} */
export const ORIGIN = 'centre';

export const MOTION = {
	/** One dot landing: the time from its first movement to rest. */
	landMs: 700,
	/** The ripple: the farthest dot starts this long after the nearest. Entry total = spreadMs + landMs (1.8 s, the brand entrance). */
	spreadMs: 1100,
	/** The dot at the ripple's origin lands first and alone: every other dot waits this share of the spread, so the first ring leaves a dot that is already well in. */
	originLead: 0.22,
	/** Exit is 70% of the entrance, the same ratio the app's exits use. */
	exitScale: 0.7,
	/** How far a dot travels along the ripple while landing, in mark units (the mark is 1024 wide). */
	driftIn: 14,
	driftOvershoot: 5,
	/** Pulse: one dot, this long, growing to this size; a new one starts every pulseMs to pulseGapMaxMs. */
	pulseMs: 3200,
	pulseGapMaxMs: 5000,
	pulseScale: 1.22,
	/** The first pulse starts this long after the entrance settles. */
	pulseFirstMs: 1200,
	/** Flow: one full trip of the palette down the C. */
	flowMs: 24000,
	/** Share of the loop the blue-to-orange seam takes up. */
	flowSeam: 0.1,
	/** Sheen: resting highlight strength, and the sweeping light's strength. */
	sheenHighlight: 0.12,
	sheenSweep: 0.26,
};

const SVG_NS = 'http://www.w3.org/2000/svg';
const EASE_LAND = 'cubic-bezier(.2, .7, .2, 1)';
const EASE_SOFT = 'ease-in-out';
const EASE_EXIT = 'cubic-bezier(.5, 0, .75, 0)';
let instanceCount = 0;

// ---- colour: oklab, so the flow blends the way the eye expects ----
/** @param {string} hex @returns {[number, number, number]} */
function hexToLab(hex) {
	const lin = (/** @type {number} */ c) => { const v = c / 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
	const r = lin(parseInt(hex.slice(1, 3), 16)), g = lin(parseInt(hex.slice(3, 5), 16)), b = lin(parseInt(hex.slice(5, 7), 16));
	const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
	const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
	const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
	return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s, 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}
/** @param {[number, number, number]} lab */
function labToHex([L, a, b]) {
	const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3, m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3, s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
	const lin = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s, -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s];
	return '#' + lin.map((v) => {
		const c = Math.min(1, Math.max(0, v));
		const e = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
		return Math.round(e * 255).toString(16).padStart(2, '0');
	}).join('');
}
/** Interpolates two oklab colours; with `wrapHue` the hue turns the long way (blue through violet and pink to orange), so the seam only uses colours already in the logo. */
/** @param {[number, number, number]} p @param {[number, number, number]} q @param {number} t @param {boolean} wrapHue @returns {[number, number, number]} */
function mixLab(p, q, t, wrapHue) {
	if (!wrapHue) return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t];
	const cp = Math.hypot(p[1], p[2]), cq = Math.hypot(q[1], q[2]);
	const hp = Math.atan2(p[2], p[1]);
	let hq = Math.atan2(q[2], q[1]);
	while (hq < hp) hq += 2 * Math.PI;   // always turn upward in hue
	const c = cp + (cq - cp) * t, h = hp + (hq - hp) * t;
	return [p[0] + (q[0] - p[0]) * t, c * Math.cos(h), c * Math.sin(h)];
}

/**
 * The palette: the logo's dots, ordered along the C, as a loop. Dot i sits at `stops[i]`; the last stretch blends the bottom blue back to the top orange.
 * @returns {{ at: (s: number) => string, stops: number[] }}
 */
function buildPalette() {
	const labs = DOTS.map((d) => hexToLab(d[3]));
	const span = 1 - MOTION.flowSeam;
	const stops = labs.map((_, i) => (i / (labs.length - 1)) * span);
	return {
		stops,
		at(s) {
			const x = ((s % 1) + 1) % 1;
			if (x >= span) return labToHex(mixLab(labs[labs.length - 1], labs[0], (x - span) / MOTION.flowSeam, true));
			const f = (x / span) * (labs.length - 1), i = Math.min(labs.length - 2, Math.floor(f));
			return labToHex(mixLab(labs[i], labs[i + 1], f - i, false));
		},
	};
}

/** @param {string} tag @param {Record<string, string | number>} attrs @returns {SVGElement} */
function svg(tag, attrs = {}) {
	const el = document.createElementNS(SVG_NS, tag);
	for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
	return el;
}

/**
 * @typedef {object} LivingLogoOptions
 * @property {'play' | 'none'} [entry] 'play' (default) lands the dots in; 'none' shows the mark at rest at once.
 * @property {number} [delay] Milliseconds the mark waits before its entry begins (the screen's own fade-rise).
 * @property {boolean} [sheen] The faint 3D highlight and the light that crosses the pulsing dot.
 * @property {keyof typeof ORIGINS} [origin] The ripple's centre.
 * @property {boolean} [reducedMotion] Forces the still mark; by default the user's prefers-reduced-motion setting decides, live.
 * @property {() => number} [random] Source for choosing the pulsing dot (tests).
 */

/**
 * Builds the mark inside `host` (an empty element that sizes it; the mark fills it, contained) and starts it.
 * @param {HTMLElement} host
 * @param {LivingLogoOptions} [options]
 */
export function createLivingLogo(host, options = {}) {
	const id = `ll${(instanceCount += 1)}`;
	const random = options.random ?? Math.random;
	const motionQuery = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
	let still = options.reducedMotion ?? motionQuery?.matches ?? false;
	const sheen = options.sheen ?? SHEEN;
	const origin = options.origin ?? ORIGIN;
	let destroyed = false, exited = false, entryDone = false, frozen = false, lastPulse = -1, pulseTimer = 0;
	/** @type {Animation[]} */ let stageAnims = [];
	/** @type {Animation[]} */ let flowAnims = [];
	/** @type {Animation[]} */ let pulseAnims = [];
	/** @type {Promise<void>} */ let entered = Promise.resolve();

	const root = svg('svg', { viewBox: '0 0 1024 1024', role: 'img', 'aria-hidden': 'true', focusable: 'false' });
	root.setAttribute('class', 'living-logo');
	root.style.cssText = 'display:block;width:100%;height:100%;overflow:visible;pointer-events:none';
	const defs = svg('defs');
	defs.innerHTML = `<radialGradient id="${id}-hi" cx=".5" cy=".5" r=".5" fx=".34" fy=".28"><stop offset="0" stop-color="#fff" stop-opacity="1"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".55"/></radialGradient>`
		+ `<linearGradient id="${id}-sweep" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity="1"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>`;
	root.appendChild(defs);

	// One <g> per dot for the entry and exit (position and opacity), and one inside it for the pulse (the dot's own size), so the two never fight over a transform.
	const palette = buildPalette();
	const parts = DOTS.map(([cx, cy, r, fill], i) => {
		const pos = svg('g');
		const grow = svg('g');
		pos.style.transformOrigin = grow.style.transformOrigin = `${cx}px ${cy}px`;
		const body = svg('circle', { cx, cy, r, fill });
		const hi = svg('circle', { cx, cy, r, fill: `url(#${id}-hi)` });
		hi.style.cssText = `opacity:0;transition:opacity 600ms ease`;
		grow.append(body, hi);
		pos.appendChild(grow);
		root.appendChild(pos);
		return { pos, grow, body, hi, cx, cy, r, i };
	});
	host.replaceChildren(root);

	/** Ripple order: how far each dot is from the origin, as a share of the farthest (0 at the origin .. 1 the farthest), and the direction it drifts in. */
	function ripple() {
		const [ox, oy] = ORIGINS[origin];
		const dist = parts.map((p) => Math.hypot(p.cx - ox, p.cy - oy));
		const far = Math.max(...dist);
		// The ripple starts where the first dot is, not at the origin's bare coordinates, so the rings follow each other without a gap.
		const near = Math.min(...dist.filter((d) => d > 1)) * 0.6;
		const first = dist.indexOf(Math.min(...dist));
		const lead = MOTION.originLead;
		return parts.map((p, i) => {
			const len = dist[i] || 1;
			const ring = Math.max(0, dist[i] - near) / (far - near);
			return { u: i === first ? 0 : lead + ring * (1 - lead), ux: (p.cx - ox) / len, uy: (p.cy - oy) / len };
		});
	}

	/** @param {number} i */
	parts.forEach((part) => { part.hi.style.opacity = sheen ? String(MOTION.sheenHighlight) : '0'; });

	/** The flow starts as the entry settles, from each dot's own colour, so the mark lands as designed and then begins to move. */
	function startFlow(/** @type {number} */ after) {
		if (still) return;
		const n = 64;
		flowAnims = parts.map((p, i) => {
			const keys = [];
			for (let k = 0; k <= n; k += 1) keys.push({ fill: palette.at(palette.stops[i] - k / n), offset: k / n });
			return p.body.animate(keys, { duration: MOTION.flowMs, iterations: Infinity, delay: after, fill: 'backwards', easing: 'linear' });
		});
	}

	/** Each dot's keyframes for landing; `out` plays them back to front with the exit's own timing. */
	function landFrames(/** @type {{ ux: number, uy: number }} */ v, /** @type {boolean} */ out) {
		const at = (/** @type {number} */ d, /** @type {number} */ s) => `translate(${(v.ux * d).toFixed(2)}px, ${(v.uy * d).toFixed(2)}px) scale(${s})`;
		if (!out) {
			return [
				{ opacity: 0, transform: at(-MOTION.driftIn, 0.3), easing: EASE_LAND, offset: 0 },
				{ opacity: 1, transform: at(MOTION.driftOvershoot, 1.12), easing: EASE_SOFT, offset: 0.5 },
				{ opacity: 1, transform: at(-1.5, 0.97), easing: EASE_SOFT, offset: 0.78 },
				{ opacity: 1, transform: at(0, 1), offset: 1 },
			];
		}
		return [
			{ opacity: 1, transform: at(0, 1), easing: EASE_SOFT, offset: 0 },
			{ opacity: 1, transform: at(-1.5, 0.97), easing: EASE_SOFT, offset: 0.22 },
			{ opacity: 1, transform: at(MOTION.driftOvershoot, 1.12), easing: EASE_EXIT, offset: 0.5 },
			{ opacity: 0, transform: at(-MOTION.driftIn, 0.3), offset: 1 },
		];
	}

	function playEntry() {
		const wait = options.delay ?? 0;
		const rip = ripple();
		stageAnims = parts.map((p, i) => p.pos.animate(landFrames(rip[i], false), { duration: MOTION.landMs, delay: wait + rip[i].u * MOTION.spreadMs, fill: 'backwards' }));
		const done = Promise.all(stageAnims.map((a) => a.finished.catch(() => undefined))).then(() => undefined);
		entered = done.then(() => { entryDone = true; if (!destroyed && !exited) schedulePulse(MOTION.pulseFirstMs); });
	}

	/** @returns {Promise<void>} resolves when the last dot has left (or at once, for the still mark). */
	function exit() {
		if (destroyed || exited) return Promise.resolve();
		exited = true;
		stopPulses();
		if (still) return Promise.resolve();
		for (const a of stageAnims) a.cancel();
		const rip = ripple();
		const land = MOTION.landMs * MOTION.exitScale, spread = MOTION.spreadMs * MOTION.exitScale;
		stageAnims = parts.map((p, i) => p.pos.animate(landFrames(rip[i], true), { duration: land, delay: (1 - rip[i].u) * spread, fill: 'both' }));
		return Promise.all(stageAnims.map((a) => a.finished.catch(() => undefined))).then(() => undefined);
	}

	// ---- pulse ----
	/** @param {number} delay */
	function schedulePulse(delay) {
		clearTimeout(pulseTimer);
		if (still || destroyed || exited || frozen || document.hidden) return;
		pulseTimer = /** @type {any} */ (setTimeout(() => pulse(), delay));
	}
	function stopPulses() { clearTimeout(pulseTimer); }

	/**
	 * Swells one dot: the dot itself grows around its own centre. With the sheen on, a soft light also slides across it.
	 * @param {number} [which] a dot index (tests and the prototype); by default a random dot other than the last.
	 */
	function pulse(which) {
		if (still || destroyed || exited) return;
		let i = which ?? -1;
		if (i < 0) do { i = Math.floor(random() * DOTS.length); } while (i === lastPulse && DOTS.length > 1);
		lastPulse = i;
		const p = parts[i];
		pulseAnims = pulseAnims.filter((a) => a.playState === 'running' || a.playState === 'paused');
		pulseAnims.push(p.grow.animate([{ transform: 'scale(1)' }, { transform: `scale(${MOTION.pulseScale})`, offset: 0.5 }, { transform: 'scale(1)' }], { duration: MOTION.pulseMs, easing: EASE_SOFT }));
		if (sheen) {
			const clip = svg('clipPath', { id: `${id}-c${i}` });
			clip.appendChild(svg('circle', { cx: p.cx, cy: p.cy, r: p.r }));
			defs.appendChild(clip);
			const band = svg('g', { 'clip-path': `url(#${id}-c${i})` });
			const tilt = svg('g', { transform: `rotate(-24 ${p.cx} ${p.cy})` });
			const light = svg('rect', { x: p.cx - p.r * 0.5, y: p.cy - p.r * 1.4, width: p.r, height: p.r * 2.8, fill: `url(#${id}-sweep)` });
			light.style.opacity = '0';
			tilt.appendChild(light);
			band.appendChild(tilt);
			p.grow.appendChild(band);
			const sweep = light.animate([
				{ opacity: 0, transform: `translateX(${-p.r * 1.6}px)`, offset: 0.1 },
				{ opacity: MOTION.sheenSweep, offset: 0.35 },
				{ opacity: MOTION.sheenSweep, offset: 0.65 },
				{ opacity: 0, transform: `translateX(${p.r * 1.6}px)`, offset: 0.9 },
			], { duration: MOTION.pulseMs, easing: 'linear', fill: 'both' });
			pulseAnims.push(sweep);
			const cleanup = () => { band.remove(); clip.remove(); };
			void sweep.finished.then(cleanup, cleanup);
		}
		schedulePulse(MOTION.pulseMs + random() * (MOTION.pulseGapMaxMs - MOTION.pulseMs));
	}

	/** Seeks every entry, exit and flow animation to `ms`, paused; `pulseMs` seeks the pulse separately. For frame capture. */
	function seek(/** @type {number} */ ms, /** @type {number | undefined} */ pulseMs) {
		frozen = true;
		stopPulses();
		for (const a of [...stageAnims, ...flowAnims]) { a.pause(); a.currentTime = ms; }
		for (const a of pulseAnims) { a.pause(); a.currentTime = pulseMs ?? ms; }
	}

	function setReduced(/** @type {boolean} */ on) {
		if (on === still || destroyed) return;
		still = on;
		if (!on) return;
		stopPulses();
		for (const a of [...stageAnims, ...flowAnims, ...pulseAnims]) a.cancel();
		stageAnims = []; flowAnims = []; pulseAnims = [];
	}
	const onMotionChange = (/** @type {MediaQueryListEvent} */ e) => { if (options.reducedMotion === undefined) setReduced(e.matches); };
	const onVisibility = () => { if (document.hidden) stopPulses(); else if (entryDone && !exited) schedulePulse(1000); };
	motionQuery?.addEventListener?.('change', onMotionChange);
	document.addEventListener('visibilitychange', onVisibility);

	if (!still) {
		const playing = (options.entry ?? 'play') === 'play';
		// With no entry to wait for the mark is settled at once, so a hidden window or tab resumes its pulse when it shows again.
		if (!playing) entryDone = true;
		startFlow(playing ? (options.delay ?? 0) + MOTION.spreadMs + MOTION.landMs : 0);
		if (playing) playEntry();
		else schedulePulse(MOTION.pulseFirstMs);
	}

	return {
		/** The mark's <svg>. */
		root,
		/** Resolves once the entry has settled (at once for the still mark or no entry). */
		get entered() { return entered; },
		/** Plays the exit and resolves when it is done. Call once, when setup hands over to the first chat. */
		exit,
		pulse,
		seek,
		/** What is animating now, for tests and the prototype's readout. */
		get state() { return { still, exited, entryAnimations: stageAnims.length, flowAnimations: flowAnims.length, pulseAnimations: pulseAnims.length, lastPulse }; },
		destroy() {
			destroyed = true;
			stopPulses();
			for (const a of [...stageAnims, ...flowAnims, ...pulseAnims]) a.cancel();
			motionQuery?.removeEventListener?.('change', onMotionChange);
			document.removeEventListener('visibilitychange', onVisibility);
			root.remove();
		},
	};
}
