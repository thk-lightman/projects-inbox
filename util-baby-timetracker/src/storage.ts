import AsyncStorage from '@react-native-async-storage/async-storage';
import { BabyEvent } from './types';

const KEY = 'baby-timetracker:events:v1';

export async function loadEvents(): Promise<BabyEvent[]> {
  const raw = await AsyncStorage.getItem(KEY);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return (parsed as BabyEvent[]).sort((a, b) => a.ts - b.ts);
  } catch {
    return [];
  }
}

export async function saveEvents(events: BabyEvent[]): Promise<void> {
  const sorted = [...events].sort((a, b) => a.ts - b.ts);
  await AsyncStorage.setItem(KEY, JSON.stringify(sorted));
}

export function genId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}
