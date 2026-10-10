import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

const buildOutput = process.env.CORSIA_BUILD_OUTPUT || 'dist';

/** @type {import('@sveltejs/kit').Config} */
const config = {
	preprocess: vitePreprocess(),
	kit: {
		adapter: adapter({
			pages: buildOutput,
			assets: buildOutput,
			fallback: 'index.html'
		})
	}
};

export default config;
