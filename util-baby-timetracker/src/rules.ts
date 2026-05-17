import { BabyEvent, EventType } from './types';

export const FEED_INTERVAL_MS = 4 * 60 * 60 * 1000;
export const WAKE_WINDOW_MS = 3.5 * 60 * 60 * 1000;

export function lastEventOf(
  events: BabyEvent[],
  types: EventType[],
): BabyEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) {
    if (types.includes(events[i].type)) return events[i];
  }
  return undefined;
}

export function nextFeedAt(events: BabyEvent[]): number | null {
  const last = lastEventOf(events, ['feed']);
  return last ? last.ts + FEED_INTERVAL_MS : null;
}

export function sleepState(events: BabyEvent[]): 'asleep' | 'awake' | 'unknown' {
  const last = lastEventOf(events, ['sleep', 'wake']);
  if (!last) return 'unknown';
  return last.type === 'sleep' ? 'asleep' : 'awake';
}

export function nextSleepAt(events: BabyEvent[]): number | null {
  const last = lastEventOf(events, ['sleep', 'wake']);
  if (!last) return null;
  if (last.type === 'wake') return last.ts + WAKE_WINDOW_MS;
  return null;
}

export function sleepToggleType(events: BabyEvent[]): 'sleep' | 'wake' {
  const last = lastEventOf(events, ['sleep', 'wake']);
  if (!last) return 'sleep';
  return last.type === 'sleep' ? 'wake' : 'sleep';
}

export function formatCountdown(targetTs: number, nowTs: number): {
  text: string;
  overdueMs: number;
} {
  const diff = targetTs - nowTs;
  const abs = Math.abs(diff);
  const h = Math.floor(abs / 3_600_000);
  const m = Math.floor((abs % 3_600_000) / 60_000);
  const sign = diff < 0 ? '-' : '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return { text: `${sign}${h}:${pad(m)}`, overdueMs: Math.max(0, -diff) };
}

export function formatClock(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
