// Runs before every page's own scripts in a headless session. Chrome's launch flags and user agent cover
// webdriver, plugins, languages and window.chrome; headless Chrome still omits performance.memory.
if (!performance.memory) {
  Object.defineProperty(performance, 'memory', {
    configurable: true,
    get: () => ({ jsHeapSizeLimit: 4294705152, totalJSHeapSize: 35839376, usedJSHeapSize: 26370128 }),
  });
}
