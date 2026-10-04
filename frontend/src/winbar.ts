import { api, type WindowState } from './hub';

export type WinMode = 'desktop' | 'browser';

const state = {
  desktop: false,
  maximized: false,
  workArea: [0, 0, 1920, 1080] as number[],
  dpr: 1,
};

export function winFlags() {
  return { desktop: state.desktop, maximized: state.maximized, workArea: state.workArea, dpr: state.dpr };
}

export async function initWindow(): Promise<WinMode> {
  try {
    const info = await api.windowState();
    state.desktop = info.desktop_mode;
    if (info.state) apply(info.state);
    if (info.desktop_mode) {
      const measure = await api.windowMeasure(window.innerHeight, window.devicePixelRatio);
      if (measure.state) apply(measure.state);
    }
  } catch {
    state.desktop = false;
  }
  return state.desktop ? 'desktop' : 'browser';
}

function apply(next: WindowState) {
  state.maximized = next.maximized;
  state.workArea = next.work_area;
  state.dpr = next.dpr || 1;
}

export async function refreshWindow() {
  if (!state.desktop) return;
  const info = await api.windowState();
  if (info.state) apply(info.state);
}

export async function windowAction(action: string, payload?: Record<string, unknown>) {
  if (!state.desktop) return { ok: false, reason: 'browser' };
  const result = await api.windowAction(action, payload);
  if (result.state) apply(result.state);
  return result;
}

/** Title-bar dragging: the OS window follows accumulated pointer movement. */
export function attachDrag(target: HTMLElement, onFrame?: (moving: boolean) => void) {
  let active = false;
  let accX = 0;
  let accY = 0;
  let queued = false;
  let lastX = 0;

  const flush = async () => {
    if (!queued) return;
    queued = false;
    const dx = accX;
    const dy = accY;
    accX = 0;
    accY = 0;
    if (!dx && !dy) return;
    await windowAction('drag', { dx, dy, ratio: clamp((lastX - state.workArea[0]) / Math.max(1, state.workArea[2] - state.workArea[0]) - 0.5, -1, 1) + 0.5 });
  };

  target.addEventListener('pointerdown', (event) => {
    if ((event.target as HTMLElement).closest('[data-nodrag]')) return;
    active = true;
    document.body.dataset.dragging = '1';
    target.setPointerCapture(event.pointerId);
    onFrame?.(true);
  });
  target.addEventListener('pointermove', (event) => {
    if (!active) return;
    accX += event.movementX;
    accY += event.movementY;
    lastX = event.screenX;
    queued = true;
  });
  const finish = async (event: PointerEvent) => {
    if (!active) return;
    active = false;
    document.body.dataset.dragging = '0';
    await flush();
    onFrame?.(false);
    const top = 6;
    const left = state.workArea[0] + 8;
    const right = state.workArea[2] - 8;
    if (event.screenY - state.workArea[1] <= top) await windowAction('maximize');
    else if (event.screenX <= left) await windowAction('snap', { side: 'left' });
    else if (event.screenX >= right) await windowAction('snap', { side: 'right' });
    await refreshWindow();
  };
  target.addEventListener('pointerup', (event) => void finish(event));
  target.addEventListener('pointercancel', () => {
    active = false;
    document.body.dataset.dragging = '0';
    onFrame?.(false);
  });
  const timer = window.setInterval(() => void flush(), 8);
  return () => window.clearInterval(timer);
}

export function attachResize(target: HTMLElement, edges: string) {
  let active = false;
  let accX = 0;
  let accY = 0;
  const flush = async () => {
    const dx = accX;
    const dy = accY;
    accX = 0;
    accY = 0;
    if (dx || dy) await windowAction('resize', { edges, dx, dy });
  };
  target.addEventListener('pointerdown', (event) => {
    active = true;
    document.body.dataset.dragging = '1';
    target.setPointerCapture(event.pointerId);
  });
  target.addEventListener('pointermove', (event) => {
    if (!active) return;
    accX += event.movementX;
    accY += event.movementY;
  });
  const stop = async () => {
    if (!active) return;
    active = false;
    document.body.dataset.dragging = '0';
    await flush();
    await refreshWindow();
  };
  target.addEventListener('pointerup', () => void stop());
  target.addEventListener('pointercancel', () => void stop());
  const timer = window.setInterval(() => void flush(), 16);
  return () => window.clearInterval(timer);
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}
