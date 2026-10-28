import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const PROBE_PAGE = readFileSync(fileURLToPath(new URL('../fixtures/browser-pane-probe.html', import.meta.url)));

/** Serves the browser-pane probe page from the E2E runner, so the product build ships no test page. */
export async function startProbeServer(): Promise<{ origin: string; popupCount: () => number; close: () => Promise<void> }> {
	let popups = 0;
	const server = createServer((request, response) => {
		if (new URL(request.url ?? '/', 'http://probe.test').searchParams.has('popup')) popups++;
		if (new URL(request.url ?? '/', 'http://probe.test').pathname !== '/browser-pane-probe.html') {
			response.statusCode = 404;
			response.end();
			return;
		}
		response.setHeader('content-type', 'text/html; charset=utf-8');
		response.end(PROBE_PAGE);
	});
	await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
	const { port } = server.address() as { port: number };
	return {
		origin: `http://127.0.0.1:${port}`,
		popupCount: () => popups,
		close: () => {
			server.closeAllConnections();
			return new Promise<void>((resolve) => server.close(() => resolve()));
		},
	};
}
