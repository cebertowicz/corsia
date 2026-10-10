<div class="drop-overlay">
	<div class="drop-zone-inner">
		<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
			<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
		</svg>
		<span>Drop files to attach</span>
	</div>
</div>

<style>
	.drop-overlay {
		position: absolute; inset: 0; z-index: 50;
		background: var(--glass-scrim); backdrop-filter: var(--glass-blur-soft); -webkit-backdrop-filter: var(--glass-blur-soft);
		display: flex; align-items: center; justify-content: center;
		pointer-events: none;
	}
	.drop-zone-inner {
		display: flex; flex-direction: column; align-items: center; gap: 12px;
		color: white; font-size: var(--text-callout); font-weight: 500;
		background: rgba(255,255,255,0.1); border: 2px dashed rgba(255,255,255,0.4);
		border-radius: var(--radius-lg); padding: 40px 60px;
	}
</style>
