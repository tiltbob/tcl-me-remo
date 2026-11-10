// Counts live intervals so the tests can prove that nothing keeps running after unmount.
// Imported first by main.tsx, before any code that could start a timer.

const live = new Set<number>();
const realSet = window.setInterval.bind(window);
const realClear = window.clearInterval.bind(window);

window.setInterval = ((handler: TimerHandler, timeout?: number, ...rest: unknown[]) => {
  const id = realSet(handler, timeout, ...rest);
  live.add(id);
  return id;
}) as typeof window.setInterval;

window.clearInterval = ((id?: number) => {
  if (id !== undefined) live.delete(id);
  realClear(id);
}) as typeof window.clearInterval;

export function liveIntervals(): number {
  return live.size;
}
