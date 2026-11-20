/**
 * A 50 ms playhead clock that a component can subscribe to on its own.
 *
 * **Why not just put it in `positionMs`.** `positionMs` is React state read by the transcript, the
 * waveform and the video track as well as the preview, so raising its update rate to 20 Hz
 * re-rendered the whole workbench 20 times a second: measured rAF **49 → 27 fps** and a
 * 232 ms → 430 ms block switch. The stage is the only consumer that needs sub-frame resolution
 * (a cue band that appears 200 ms late is visible; a highlighted word that does is not), so the
 * fine clock is a subscription instead of a prop.
 *
 * `useSyncExternalStore` is the intended reader; the value is a plain number so React can skip a
 * render when it has not changed.
 */
class PlayheadClock {
  private listeners = new Set<() => void>();
  private value = 0;

  publish(ms: number): void {
    if (ms === this.value) return;
    this.value = ms;
    for (const listener of this.listeners) listener();
  }

  /** A jump (seek, pause, play from elsewhere) is not a tick: publish unconditionally. */
  reset(ms: number): void {
    this.value = ms;
    for (const listener of this.listeners) listener();
  }

  read = (): number => this.value;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
}

export const playheadClock = new PlayheadClock();
