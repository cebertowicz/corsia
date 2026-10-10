<section class="block">
	<header class="block-head">
		<h2>Glass materials</h2>
		<p>
			Apple Liquid Glass (HIG) defines four material levels by opacity.
			Different surfaces use different levels — never apply the same
			glass everywhere. Pick the level that matches the surface's
			role: high opacity for persistent / readability-critical
			surfaces, low opacity for fleeting overlays.
		</p>
	</header>
	<div class="materials">
		<div class="material material--thin">
			<div class="m-tag">Thin · 50%</div>
			<div class="m-use">
				<strong>Title bars, headers, input bars.</strong>
				Content scrolls under cleanly; blur dominates over fill.
				Used on: <code>.main-header</code>, <code>.input-area</code>.
			</div>
		</div>
		<div class="material material--regular">
			<div class="m-tag">Regular · 60%</div>
			<div class="m-use">
				<strong>Drawers, slide-outs.</strong>
				Surfaces that overlay content with their own dense info.
				Used on: <code>TimelinePanel</code>.
			</div>
		</div>
		<div class="material material--regularThick">
			<div class="m-tag">Regular Thick · 65%</div>
			<div class="m-use">
				<strong>Modals, dropdowns, popovers.</strong>
				Focus surfaces. Readability matters; blur the background
				but keep panel text crisp. Used on: <code>Modal</code>,
				<em>(coming) model selector dropdown, picker dropdowns,
				context menu</em>.
			</div>
		</div>
		<div class="material material--thick">
			<div class="m-tag">Thick · 75%</div>
			<div class="m-use">
				<strong>Sidebars, persistent navigation.</strong>
				Always-visible surfaces with high readability demands.
				Used on: <code>.sidebar</code>.
			</div>
		</div>
		<div class="material material--solid">
			<div class="m-tag">Solid · 100%</div>
			<div class="m-use">
				<strong>Message bubbles, code blocks, message list bg.</strong>
				Content surfaces that demand full legibility — no
				vibrancy, no blur. Used on: <code>.main</code>,
				<code>.message</code>, code <code>&lt;pre&gt;</code> blocks.
			</div>
		</div>
	</div>
</section>

<style>
	.block {
		margin-bottom: 32px;
	}
	.block-head {
		margin-bottom: 12px;
	}
	.block-head h2 {
		font-size: var(--text-body); font-weight: 600;
		letter-spacing: -0.01em;
		color: var(--text-primary);
		margin: 0 0 4px;
	}
	.block-head p {
		font-size: var(--text-subhead);
		color: var(--text-tertiary);
		margin: 0;
		line-height: 1.5;
	}
	.materials { display: flex; flex-direction: column; gap: 8px; }
	.material {
		padding: 14px 16px;
		border-radius: var(--radius-sm);
		display: grid;
		grid-template-columns: 160px 1fr;
		gap: 16px; align-items: start;
		font-size: var(--text-subhead);
		backdrop-filter: blur(40px) saturate(180%);
		-webkit-backdrop-filter: blur(40px) saturate(180%);
	}
	.material .m-tag {
		font-size: var(--text-caption); font-weight: 700;
		letter-spacing: 0.12em; text-transform: uppercase;
		color: var(--text-tertiary);
	}
	.material .m-use { color: var(--text-secondary); line-height: 1.5; }
	.material .m-use strong { color: var(--text-primary); display: block; margin-bottom: 4px; }
	.material .m-use code { font-family: 'SF Mono', monospace; font-size: var(--text-footnote); color: var(--text-tertiary); }

	.material--thin         { background: color-mix(in srgb, var(--bg-secondary) 50%, transparent); }
	.material--regular      { background: color-mix(in srgb, var(--bg-secondary) 60%, transparent); }
	.material--regularThick { background: color-mix(in srgb, var(--bg-secondary) 65%, transparent); }
	.material--thick        { background: color-mix(in srgb, var(--bg-secondary) 75%, transparent); }
	.material--solid        { background: var(--bg-secondary); backdrop-filter: none; -webkit-backdrop-filter: none; }
</style>
