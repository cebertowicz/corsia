import { get } from "svelte/store";
import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AdaptiveDrawerHarness from "./fixtures/AdaptiveDrawerHarness.svelte";

// The reduced-motion preference, read when the drawer subscribes; false unless a test sets it.
const reduced = vi.hoisted(() => ({ current: false }));
vi.mock("$lib/stores/viewport", async (importOriginal) => {
  const { readable } = await import("svelte/store");
  return {
    ...(await importOriginal<typeof import("$lib/stores/viewport")>()),
    prefersReducedMotion: { subscribe: (run: (value: boolean) => void) => readable(reduced.current).subscribe(run) },
  };
});

let host: HTMLElement;
let component: Record<string, unknown> | null;

async function settle() {
  await tick();
  flushSync();
  await Promise.resolve();
  await tick();
  flushSync();
}

function pointer(type: string, clientY: number, pointerId = 7): Event {
  const event = new Event(type, { bubbles: true });
  Object.defineProperties(event, {
    button: { value: 0 },
    clientY: { value: clientY },
    pointerId: { value: pointerId },
  });
  return event;
}

beforeEach(() => {
  // The drawer portals to document.body, so the host is the body itself.
  host = document.body;
  component = null;
});

afterEach(() => {
  reduced.current = false;
  if (component) unmount(component);
  component = null;
  host.replaceChildren();
  vi.restoreAllMocks();
});

describe("AdaptiveDrawer", () => {
  it("keeps content-owned context beside Done at desktop width", async () => {
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: vi.fn(), contextLabel: "Decision · Reviewer subagent · CORSIA" },
    });
    await settle();

    const header = host.querySelector(".drawer-head") as HTMLElement;
    expect(header.querySelector("h2")).toBeNull();
    expect(header.querySelector(".drawer-context")?.textContent).toBe("Decision · Reviewer subagent · CORSIA");
    expect(header.querySelectorAll(".drawer-context")).toHaveLength(1);
    expect(header.querySelector(".drawer-done")?.textContent?.trim()).toBe("Done");
    expect(header.querySelector(".drawer-context + .drawer-done")).toBeTruthy();
  });

  it("gives its content a bounded flex column only when asked to fill", async () => {
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn() } });
    await settle();
    expect(host.querySelector(".drawer-body")?.classList.contains("fill")).toBe(false);
    unmount(component);
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn(), fill: true } });
    await settle();
    expect(host.querySelector(".drawer-body")?.classList.contains("fill")).toBe(true);
  });

  it("sits on the visible part of the screen only when asked to follow the keyboard", async () => {
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn() } });
    await settle();
    expect(host.querySelector(".drawer-backdrop")?.classList.contains("follow-keyboard")).toBe(false);
    unmount(component);
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn(), followKeyboard: true } });
    await settle();
    expect(host.querySelector(".drawer-backdrop")?.classList.contains("follow-keyboard")).toBe(true);
  });

  it("owns one modal dialog and moves focus to the consumer target", async () => {
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: vi.fn() },
    });
    await settle();

    const dialog = host.querySelector('[role="dialog"]');
    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    expect(dialog?.getAttribute("aria-label")).toBe("Focused action");
    expect(document.activeElement?.textContent).toBe("Primary action");
    expect(host.querySelectorAll("[data-adaptive-drawer]")).toHaveLength(1);
  });

  it("can keep a sheet non-modal without changing the modal default", async () => {
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: vi.fn(), modal: false },
    });
    await settle();

    const backdrop = host.querySelector("[data-adaptive-drawer]");
    const dialog = host.querySelector('[role="dialog"]');
    expect(backdrop?.classList.contains("nonmodal")).toBe(true);
    expect(dialog?.getAttribute("aria-modal")).toBe("false");
  });

  it("keeps a half-height RSR-8 sheet non-modal so the visible document remains interactive", async () => {
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: vi.fn(), detent: "half" },
    });
    await settle();

    const backdrop = host.querySelector("[data-adaptive-drawer]");
    const dialog = host.querySelector('[role="dialog"]');
    expect(backdrop?.classList.contains("half")).toBe(true);
    expect(backdrop?.classList.contains("nonmodal")).toBe(true);
    expect(dialog?.getAttribute("aria-modal")).toBe("false");
  });

  it("stays open but hidden and out of reach while something opened from it covers it", async () => {
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn() } });
    await settle();
    const uncovered = host.querySelector("[data-adaptive-drawer]") as HTMLElement;
    expect(uncovered.classList.contains("covered")).toBe(false);
    expect(uncovered.inert).toBeFalsy();
    unmount(component);
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn(), covered: true } });
    await settle();
    // Still mounted, so its scroll and place are kept for when it is uncovered.
    const covered = host.querySelector("[data-adaptive-drawer]") as HTMLElement;
    expect(covered.querySelector('[role="dialog"]')).not.toBeNull();
    expect(covered.classList.contains("covered")).toBe(true);
    expect(covered.inert).toBe(true);
  });

  it("calls onClosed once after the local outro", async () => {
    const closed = vi.fn();
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { callerControlled: true, onClose: closed },
    });
    await settle();

    (host.querySelector("[data-drawer-trigger]") as HTMLButtonElement).click();
    await settle();
    (host.querySelector(".drawer-done") as HTMLButtonElement).click();
    await settle();

    expect(closed).toHaveBeenCalledTimes(1);
    expect(host.querySelectorAll("[data-adaptive-drawer]")).toHaveLength(0);
  });

  it("under reduced motion keeps the drawer through the backdrop's crossfade, then calls onClosed once", async () => {
    // jsdom's stand-in animations end at once; these end after their duration, on timers the test advances.
    const instant = Element.prototype.animate;
    Element.prototype.animate = function animate(_keyframes, options) {
      const duration = typeof options === "number" ? options : Number(options?.duration ?? 0);
      const animation = { cancel() {}, finish() {}, currentTime: 0, playState: "running" } as unknown as Animation;
      Object.defineProperty(animation, "onfinish", {
        set(callback: ((event: Event) => void) | null) {
          if (callback) setTimeout(() => callback(new Event("finish")), duration);
        },
      });
      return animation;
    };
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      reduced.current = true;
      document.documentElement.style.setProperty("--dur-tight", "120ms");
      const closed = vi.fn();
      component = mount(AdaptiveDrawerHarness, { target: host, props: { callerControlled: true, onClose: closed } });
      await settle();
      (host.querySelector("[data-drawer-trigger]") as HTMLButtonElement).click();
      await settle();
      await vi.advanceTimersByTimeAsync(200);
      const backdrop = host.querySelector("[data-adaptive-drawer]") as HTMLElement;

      (host.querySelector(".drawer-done") as HTMLButtonElement).click();
      await settle();
      await vi.advanceTimersByTimeAsync(60);
      // Mid-crossfade: the backdrop is still on screen fading, and the drawer is not closed yet.
      expect(backdrop.isConnected).toBe(true);
      expect(closed).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(80);
      await settle();
      expect(closed).toHaveBeenCalledTimes(1);
      expect(host.querySelectorAll("[data-adaptive-drawer]")).toHaveLength(0);
    } finally {
      vi.useRealTimers();
      Element.prototype.animate = instant;
      document.documentElement.style.removeProperty("--dur-tight");
    }
  });

  it("closes from its explicit control and Escape", async () => {
    const close = vi.fn();
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: close },
    });
    await settle();

    // The canonical chrome dismisses from a text "Done" control whose visible
    // label is also its accessible name, not a corner close glyph.
    const done = host.querySelector(".drawer-done") as HTMLButtonElement;
    expect(done.textContent?.trim()).toBe("Done");
    done.click();
    expect(close).toHaveBeenCalledTimes(1);

    host
      .querySelector('[role="dialog"]')
      ?.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    expect(close).toHaveBeenCalledTimes(2);
  });

  it("closes only from the backdrop itself, never a click inside the panel", async () => {
    const close = vi.fn();
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: close },
    });
    await settle();

    (host.querySelector(".drawer-panel") as HTMLElement).click();
    expect(close).not.toHaveBeenCalled();

    (host.querySelector("[data-adaptive-drawer]") as HTMLElement).click();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("supports a deliberate downward drag to dismiss and snaps short drags back", async () => {
    const close = vi.fn();
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: close, tall: true },
    });
    await settle();

    const handle = host.querySelector(".drawer-drag-region") as HTMLElement;
    handle.dispatchEvent(pointer("pointerdown", 100));
    handle.dispatchEvent(pointer("pointermove", 135));
    handle.dispatchEvent(pointer("pointerup", 135));
    expect(close).not.toHaveBeenCalled();
    expect(
      (
        host.querySelector(".drawer-panel") as HTMLElement
      ).style.getPropertyValue("--drawer-drag-y"),
    ).toBe("0px");

    handle.dispatchEvent(pointer("pointerdown", 100, 8));
    handle.dispatchEvent(pointer("pointermove", 280, 8));
    handle.dispatchEvent(pointer("pointerup", 280, 8));
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("keeps a dedicated independently scrollable body for long content", async () => {
    component = mount(AdaptiveDrawerHarness, {
      target: host,
      props: { onClose: vi.fn(), tall: true },
    });
    await settle();

    expect(host.querySelector(".drawer-body .tall-content")).toBeTruthy();
    expect(
      host.querySelector(".drawer-panel")?.classList.contains("dragging"),
    ).toBe(false);
  });
});
