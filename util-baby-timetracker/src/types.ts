export type EventType = 'feed' | 'sleep' | 'wake' | 'diaper' | 'lotion';

export interface BabyEvent {
  id: string;
  type: EventType;
  ts: number;
}

export const EVENT_META: Record<
  EventType,
  { label: string; emoji: string; color: string }
> = {
  feed: { label: '밥', emoji: '🍼', color: '#4ade80' },
  sleep: { label: '잠들기', emoji: '😴', color: '#60a5fa' },
  wake: { label: '깸', emoji: '☀️', color: '#fbbf24' },
  diaper: { label: '기저귀', emoji: '🧷', color: '#f472b6' },
  lotion: { label: '로션', emoji: '🧴', color: '#a78bfa' },
};
