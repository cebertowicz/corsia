import { expect, test } from '@playwright/test';
import { createServer } from 'node:http';
import { createSession, loginByUrl } from '../support/corsia';

test.use({ video: 'on' });

test.describe('@flow:browser-pane animated browser control', () => {
 test('delivers tile clicks and touch scrolling during animation', async ({ page, request, context }, testInfo) => {
  test.setTimeout(90_000);
  const mobile = testInfo.project.name.includes('mobile');
  const reports: { total: number; scrollY: number; ready?: boolean; released?: boolean }[] = [];
  const server = createServer((req, res) => {
   let body = ''; req.on('data', chunk => body += chunk);
   req.on('end', () => { try { reports.push(JSON.parse(body)); } catch { /* Ignore unrelated requests. */ } res.end('ok'); });
  });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as { port: number }).port;
  let frameCount = 0, frameBytes = 0, remoteWidth = 0, remoteHeight = 0;
  const started = Date.now();
  try {
   // Keep real touch enabled and enter the browser through the phone's own controls.
   if (mobile) await page.setViewportSize({ width: 393, height: 851 });
   const title = `pane-stress-${Date.now()}`;
   const threadId = await createSession(request, title);
   page.on('websocket', socket => {
    if (!socket.url().includes(`/api/browser/${threadId}/stream`)) return;
    socket.on('framereceived', frame => {
     const data = JSON.parse(String(frame.payload));
     if (data.type !== 'frame') return;
     frameCount++; frameBytes += String(frame.payload).length;
     remoteWidth = data.metadata.deviceWidth; remoteHeight = data.metadata.deviceHeight;
    });
   });
   await loginByUrl(page);
   if (mobile) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
   await page.getByText(title).first().click();
   await page.evaluate(build => {
    const stamp = document.createElement('div');
    stamp.textContent = `Pane build: ${build}`;
    stamp.style.cssText = 'position:fixed;top:2px;right:2px;z-index:9999;background:#111;color:white;font:11px monospace;pointer-events:none';
    document.body.append(stamp);
   }, process.env.CORSIA_PANE_BUILD ?? 'unknown');
   await page.getByRole('button', { name: 'Open browser', exact: true }).click();
   const address = page.getByRole('textbox', { name: 'Browser address' });
   const probe = new URL('/browser-pane-probe.html', page.url());
   probe.searchParams.set('stress', '1'); probe.searchParams.set('report', `http://127.0.0.1:${port}`);
   await address.fill(probe.href);
   await page.getByRole('button', { name: 'Go', exact: true }).click();
   const canvas = page.locator('canvas[aria-label="Shared browser"]');
   await page.emulateMedia({ colorScheme: 'dark' });
   await expect.poll(() => reports.some(report => report.ready)).toBe(true);
   // Startup is reported separately; compare input latency once each pane is drawing.
   await expect.poll(async () => Math.abs(remoteWidth - (await canvas.boundingBox())!.width), { timeout: 30_000 }).toBeLessThan(2);
   await expect.poll(() => frameCount, { timeout: 30_000 }).toBeGreaterThan(2);
   await page.getByRole('button', { name: 'Take over', exact: true }).click();
   await expect(page.getByText('You have control. The agent waits')).toBeVisible();
   await canvas.evaluate((node, sampleTarget) => {
    const c = node as HTMLCanvasElement;
    const state = { samples: [] as number[], target: sampleTarget, blank: 0, ticks: 0, seen: false, down: 0, before: [] as number[], end: performance.now() + 10000 };
    (window as unknown as { paneMeasurement: typeof state }).paneMeasurement = state;
    const pixel = () => Array.from(c.getContext('2d')!.getImageData(Math.floor(c.width * .05), Math.floor(c.height * .5), 1, 1).data);
    c.addEventListener('pointerdown', () => {
     if (state.samples.length < state.target) { state.before = pixel(); state.down = performance.now(); }
    });
    function sample() {
     const now = performance.now(), color = pixel();
     if (now < state.end) {
      state.ticks++;
      if (state.seen && color[3] === 0) state.blank++;
     }
     if (color[3] > 0) state.seen = true;
     if (state.down && color[3] > 0 && Math.abs(color[0] - state.before[0]) > 80) { state.samples.push(now - state.down); state.down = 0; }
     if (now < state.end || state.samples.length < state.target) requestAnimationFrame(sample);
    }
    requestAnimationFrame(sample);
   }, mobile ? 0 : 30);
   const bounds = (await canvas.boundingBox())!;
   if (mobile) {
    const session = await context.newCDPSession(page);
    const x = bounds.x + bounds.width * .5, y = bounds.y + Math.min(500, bounds.height - 40);
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
    for (let i = 1; i <= 10; i++) {
     await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - i * 20 }] });
     await page.waitForTimeout(20);
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await expect.poll(() => reports.at(-1)?.scrollY ?? 0).toBeGreaterThan(160);
    expect(reports.at(-1)!.scrollY).toBeLessThan(240);
    await page.touchscreen.tap(bounds.x + 60, bounds.y + 60);
    await expect.poll(() => reports.at(-1)?.total).toBe(1);
   } else {
    const firstFrame = frameCount, firstBytes = frameBytes, firstTime = Date.now();
    for (let i = 0; i < 50; i++) {
     const x = bounds.x + (i === 49 ? 50 : 60 + (i % 3) * 44);
     const y = bounds.y + (i === 49 ? 200 : 60 + (Math.floor(i / 3) % 3) * 44);
     await page.mouse.click(x, y);
     await expect.poll(() => reports.at(-1)?.total).toBe(i + 1);
     if (i < 30) await expect.poll(() => canvas.evaluate(() => (window as unknown as { paneMeasurement: { samples: number[] } }).paneMeasurement.samples.length)).toBe(i + 1);
    }
    await page.mouse.move(bounds.x + bounds.width * .5, bounds.y + bounds.height * .5);
    await page.mouse.wheel(0, 200);
    await expect.poll(() => reports.at(-1)?.scrollY ?? 0).toBeGreaterThan(160);
    await page.mouse.wheel(0, -200);
    await page.waitForTimeout(10000);
    const measurement = await canvas.evaluate(() => (window as unknown as { paneMeasurement: { samples: number[]; blank: number; ticks: number } }).paneMeasurement);
    const sorted = measurement.samples.slice(0,30).sort((a,b) => a-b);
    const elapsed = (Date.now() - firstTime) / 1000;
    await testInfo.attach('pane-measurements', { body: JSON.stringify({ ...measurement, p50: sorted[14], p95: sorted[28], fps: (frameCount-firstFrame)/elapsed, bytesPerFrame: (frameBytes-firstBytes)/(frameCount-firstFrame), clicks: reports.at(-1)?.total, build: process.env.CORSIA_PANE_BUILD, remoteWidth, remoteHeight, started }), contentType: 'application/json' });
    expect(measurement.blank).toBe(0);
    const releases = reports.filter(report => report.released).length;
    await page.mouse.move(bounds.x + 20, bounds.y + bounds.height - 20);
    await page.mouse.down();
    await page.mouse.move(bounds.x + bounds.width + 20, bounds.y + bounds.height + 10);
    await page.mouse.up();
    await expect.poll(() => reports.filter(report => report.released).length).toBe(releases + 1);
   }
   await page.screenshot({ path: testInfo.outputPath('pane-proof.png') });
  } finally { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); }
 });
});
