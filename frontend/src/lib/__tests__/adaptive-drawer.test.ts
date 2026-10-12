import { get } from "svelte/store";
import { flushSync, mount, tick, unmount } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toastClearance } from "$lib/stores/toastClearance";
import AdaptiveDrawerHarness from "./fixtures/AdaptiveDrawerHarness.svelte";

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
  if (component) unmount(component);
  component = null;
  host.replaceChildren();
  vi.restoreAllMocks();
});

describe("AdaptiveDrawer", () => {
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

  it("registers desktop resting occupancy including its bottom margin and releases it at close start", async () => {
    component = mount(AdaptiveDrawerHarness, { target: host, props: { onClose: vi.fn() } });
    await settle();

    const panel = host.querySelector(".drawer-panel") as HTMLElement;
    panel.style.marginBottom = "16px";
    Object.defineProperty(panel, "offsetHeight", { configurable: true, value: 440 });
    window.dispatchEvent(new Event("resize"));

    expect(get(toastClearance)).toBe(456);

    (host.querySelector("[data-adaptive-drawer]") as HTMLElement).dispatchEvent(new Event("outrostart"));
    expect(get(toastClearance)).toBe(0);
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
