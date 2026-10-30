import { execFileSync } from 'node:child_process';
import { defineConfig, mergeConfig } from 'vite';
import config from '../../vite.config';

// A second disposable frontend uses the exact base versions of the two changed components.
// It shares only the mock backend with the patched frontend, so the same seeded data is rendered by both.
export default defineConfig((env) => mergeConfig(config(env), {
	plugins: [{
		name: 'comment-cards-base-components',
		enforce: 'pre',
		load(id) {
			const match = id.match(/\/src\/lib\/components\/comments\/(CommentList|CommentThread)\.svelte$/);
			if (!match) return null;
			return execFileSync('git', ['show', `a9747200c:frontend/src/lib/components/comments/${match[1]}.svelte`], { encoding: 'utf8' });
		},
	}],
}));
