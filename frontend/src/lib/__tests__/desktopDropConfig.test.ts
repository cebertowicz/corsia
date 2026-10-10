import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { describe, expect, it } from 'vitest';

const tauriConfigPath = resolve(process.cwd(), '../desktop/src-tauri/tauri.conf.json');

describe('desktop file drops', () => {
	it('leaves file-drop delivery to the browser DOM route', () => {
		const config = JSON.parse(readFileSync(tauriConfigPath, 'utf8')) as {
			app: { windows: Array<{ dragDropEnabled?: boolean }> };
		};

		expect(config.app.windows[0]?.dragDropEnabled).toBe(false);
	});
});
