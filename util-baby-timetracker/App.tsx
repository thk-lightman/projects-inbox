import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  FlatList,
  Modal,
  Pressable,
  SafeAreaView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';

import { BabyEvent, EVENT_META, EventType } from './src/types';
import { genId, loadEvents, saveEvents } from './src/storage';
import {
  formatClock,
  formatCountdown,
  nextFeedAt,
  nextSleepAt,
  sleepState,
  sleepToggleType,
} from './src/rules';

type DraftEdit = { id: string; hh: string; mm: string };

const TICK_MS = 30 * 1000;

export default function App() {
  const [events, setEvents] = useState<BabyEvent[]>([]);
  const [now, setNow] = useState<number>(Date.now());
  const [edit, setEdit] = useState<DraftEdit | null>(null);
  const undoRef = useRef<BabyEvent | null>(null);

  useEffect(() => {
    loadEvents().then(setEvents).catch(() => setEvents([]));
  }, []);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, []);

  const persist = useCallback(async (next: BabyEvent[]) => {
    setEvents(next);
    await saveEvents(next);
  }, []);

  const addEvent = useCallback(
    async (type: EventType) => {
      const ev: BabyEvent = { id: genId(), type, ts: Date.now() };
      const next = [...events, ev];
      undoRef.current = ev;
      await persist(next);
    },
    [events, persist],
  );

  const onSleepPress = useCallback(() => {
    const type = sleepToggleType(events);
    addEvent(type);
  }, [events, addEvent]);

  const undo = useCallback(async () => {
    const target = undoRef.current;
    if (!target) {
      Alert.alert('되돌릴 항목 없음');
      return;
    }
    const next = events.filter((e) => e.id !== target.id);
    undoRef.current = null;
    await persist(next);
  }, [events, persist]);

  const removeEvent = useCallback(
    async (id: string) => {
      const next = events.filter((e) => e.id !== id);
      await persist(next);
    },
    [events, persist],
  );

  const saveEdit = useCallback(async () => {
    if (!edit) return;
    const h = parseInt(edit.hh, 10);
    const m = parseInt(edit.mm, 10);
    if (Number.isNaN(h) || Number.isNaN(m) || h < 0 || h > 23 || m < 0 || m > 59) {
      Alert.alert('시간 형식 오류', 'HH:MM 형식으로 입력');
      return;
    }
    const target = events.find((e) => e.id === edit.id);
    if (!target) return;
    const d = new Date(target.ts);
    d.setHours(h, m, 0, 0);
    if (d.getTime() > Date.now()) d.setDate(d.getDate() - 1);
    const next = events.map((e) => (e.id === edit.id ? { ...e, ts: d.getTime() } : e));
    setEdit(null);
    await persist(next);
  }, [edit, events, persist]);

  const feedTarget = useMemo(() => nextFeedAt(events), [events]);
  const sleepTarget = useMemo(() => nextSleepAt(events), [events]);
  const sState = useMemo(() => sleepState(events), [events]);

  const recent = useMemo(() => [...events].slice(-10).reverse(), [events]);

  return (
    <SafeAreaView style={styles.root}>
      <StatusBar barStyle="light-content" />
      <View style={styles.header}>
        <Text style={styles.title}>BabyTimeTracker</Text>
        <Text style={styles.clock}>{formatClock(now)}</Text>
      </View>

      <View style={styles.hudRow}>
        <HudCard
          label="다음 밥"
          emoji="🍼"
          targetTs={feedTarget}
          now={now}
          fallback="첫 기록 대기"
        />
        <HudCard
          label={sState === 'asleep' ? '자는 중' : '다음 잠'}
          emoji={sState === 'asleep' ? '😴' : '🌙'}
          targetTs={sState === 'asleep' ? null : sleepTarget}
          now={now}
          fallback={sState === 'asleep' ? '깨우기 입력 대기' : '첫 기록 대기'}
          dim={sState === 'asleep'}
        />
      </View>

      <View style={styles.btnGrid}>
        <BigBtn type="feed" onPress={() => addEvent('feed')} />
        <BigBtn type={sState === 'asleep' ? 'wake' : 'sleep'} onPress={onSleepPress} />
        <BigBtn type="diaper" onPress={() => addEvent('diaper')} />
        <BigBtn type="lotion" onPress={() => addEvent('lotion')} />
      </View>

      <Pressable style={styles.undoBtn} onPress={undo}>
        <Text style={styles.undoText}>↶ 마지막 입력 되돌리기</Text>
      </Pressable>

      <View style={styles.logHeader}>
        <Text style={styles.logTitle}>최근 기록</Text>
        <Text style={styles.logHint}>탭=시간수정 · 길게=삭제</Text>
      </View>
      <FlatList
        data={recent}
        keyExtractor={(it) => it.id}
        renderItem={({ item }) => (
          <LogRow
            item={item}
            onEdit={() => {
              const d = new Date(item.ts);
              setEdit({
                id: item.id,
                hh: String(d.getHours()).padStart(2, '0'),
                mm: String(d.getMinutes()).padStart(2, '0'),
              });
            }}
            onDelete={() =>
              Alert.alert('삭제', `${EVENT_META[item.type].label} ${formatClock(item.ts)} 삭제?`, [
                { text: '취소', style: 'cancel' },
                { text: '삭제', style: 'destructive', onPress: () => removeEvent(item.id) },
              ])
            }
          />
        )}
        ListEmptyComponent={
          <Text style={styles.empty}>아직 기록 없음. 위 버튼을 눌러 시작</Text>
        }
        contentContainerStyle={{ paddingBottom: 24 }}
      />

      <Modal visible={!!edit} transparent animationType="fade" onRequestClose={() => setEdit(null)}>
        <View style={styles.modalRoot}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>시간 수정</Text>
            <View style={styles.timeRow}>
              <TextInput
                style={styles.timeInput}
                value={edit?.hh ?? ''}
                onChangeText={(v) => setEdit((s) => (s ? { ...s, hh: v } : s))}
                keyboardType="number-pad"
                maxLength={2}
              />
              <Text style={styles.timeColon}>:</Text>
              <TextInput
                style={styles.timeInput}
                value={edit?.mm ?? ''}
                onChangeText={(v) => setEdit((s) => (s ? { ...s, mm: v } : s))}
                keyboardType="number-pad"
                maxLength={2}
              />
            </View>
            <View style={styles.modalBtnRow}>
              <Pressable style={[styles.modalBtn, styles.modalBtnGhost]} onPress={() => setEdit(null)}>
                <Text style={styles.modalBtnGhostText}>취소</Text>
              </Pressable>
              <Pressable style={[styles.modalBtn, styles.modalBtnPrimary]} onPress={saveEdit}>
                <Text style={styles.modalBtnPrimaryText}>저장</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

function HudCard({
  label,
  emoji,
  targetTs,
  now,
  fallback,
  dim,
}: {
  label: string;
  emoji: string;
  targetTs: number | null;
  now: number;
  fallback: string;
  dim?: boolean;
}) {
  if (targetTs == null) {
    return (
      <View style={[styles.hudCard, styles.hudCardIdle]}>
        <Text style={styles.hudLabel}>
          {emoji} {label}
        </Text>
        <Text style={styles.hudFallback}>{fallback}</Text>
      </View>
    );
  }
  const { text, overdueMs } = formatCountdown(targetTs, now);
  const overdue = overdueMs > 0;
  return (
    <View
      style={[
        styles.hudCard,
        overdue ? styles.hudCardOverdue : dim ? styles.hudCardDim : styles.hudCardActive,
      ]}
    >
      <Text style={styles.hudLabel}>
        {emoji} {label}
      </Text>
      <Text style={[styles.hudCountdown, overdue && styles.hudOverdueText]}>{text}</Text>
      <Text style={styles.hudTarget}>{formatClock(targetTs)}</Text>
    </View>
  );
}

function BigBtn({ type, onPress }: { type: EventType; onPress: () => void }) {
  const meta = EVENT_META[type];
  return (
    <Pressable
      onPress={onPress}
      style={({ pressed }) => [
        styles.bigBtn,
        { borderColor: meta.color },
        pressed && styles.bigBtnPressed,
      ]}
    >
      <Text style={styles.bigBtnEmoji}>{meta.emoji}</Text>
      <Text style={styles.bigBtnLabel}>{meta.label}</Text>
    </Pressable>
  );
}

function LogRow({
  item,
  onEdit,
  onDelete,
}: {
  item: BabyEvent;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const meta = EVENT_META[item.type];
  return (
    <Pressable onPress={onEdit} onLongPress={onDelete} style={styles.logRow}>
      <Text style={[styles.logDot, { color: meta.color }]}>●</Text>
      <Text style={styles.logLabel}>
        {meta.emoji} {meta.label}
      </Text>
      <Text style={styles.logTime}>{formatClock(item.ts)}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: '#0b0b0f', paddingHorizontal: 16 },
  header: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingVertical: 12,
  },
  title: { color: '#e5e7eb', fontSize: 18, fontWeight: '600' },
  clock: { color: '#9ca3af', fontSize: 16, fontVariant: ['tabular-nums'] },
  hudRow: { flexDirection: 'row', gap: 12, marginBottom: 16 },
  hudCard: {
    flex: 1,
    borderRadius: 14,
    paddingVertical: 18,
    paddingHorizontal: 14,
    borderWidth: 1,
  },
  hudCardIdle: { backgroundColor: '#16161d', borderColor: '#26262f' },
  hudCardDim: { backgroundColor: '#1a1d2a', borderColor: '#2d3346' },
  hudCardActive: { backgroundColor: '#1a2a1d', borderColor: '#2f4f37' },
  hudCardOverdue: { backgroundColor: '#3a1414', borderColor: '#7a2a2a' },
  hudLabel: { color: '#cbd5e1', fontSize: 14, marginBottom: 6 },
  hudCountdown: {
    color: '#f3f4f6',
    fontSize: 32,
    fontWeight: '700',
    fontVariant: ['tabular-nums'],
  },
  hudOverdueText: { color: '#fecaca' },
  hudTarget: { color: '#9ca3af', fontSize: 13, marginTop: 2 },
  hudFallback: { color: '#6b7280', fontSize: 14 },
  btnGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 12, marginBottom: 12 },
  bigBtn: {
    width: '47%',
    aspectRatio: 1.6,
    backgroundColor: '#16161d',
    borderRadius: 16,
    borderWidth: 2,
    alignItems: 'center',
    justifyContent: 'center',
  },
  bigBtnPressed: { opacity: 0.6, transform: [{ scale: 0.97 }] },
  bigBtnEmoji: { fontSize: 34 },
  bigBtnLabel: { color: '#e5e7eb', fontSize: 16, marginTop: 4, fontWeight: '600' },
  undoBtn: { alignSelf: 'center', paddingVertical: 10, paddingHorizontal: 16, marginBottom: 8 },
  undoText: { color: '#9ca3af', fontSize: 14 },
  logHeader: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    paddingTop: 6,
    paddingBottom: 4,
    borderTopWidth: 1,
    borderTopColor: '#1f1f28',
  },
  logTitle: { color: '#cbd5e1', fontSize: 14, fontWeight: '600' },
  logHint: { color: '#6b7280', fontSize: 11 },
  empty: { color: '#6b7280', textAlign: 'center', paddingVertical: 24 },
  logRow: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: 10,
    paddingHorizontal: 6,
    borderBottomWidth: 1,
    borderBottomColor: '#16161d',
  },
  logDot: { fontSize: 14, marginRight: 8 },
  logLabel: { color: '#e5e7eb', fontSize: 15, flex: 1 },
  logTime: { color: '#9ca3af', fontSize: 15, fontVariant: ['tabular-nums'] },
  modalRoot: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  modalCard: {
    backgroundColor: '#1a1a23',
    borderRadius: 16,
    padding: 20,
    width: '100%',
    maxWidth: 320,
  },
  modalTitle: { color: '#e5e7eb', fontSize: 16, fontWeight: '600', marginBottom: 14 },
  timeRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 18,
  },
  timeInput: {
    width: 70,
    backgroundColor: '#0b0b0f',
    borderRadius: 10,
    borderWidth: 1,
    borderColor: '#2d2d3a',
    color: '#f3f4f6',
    fontSize: 28,
    textAlign: 'center',
    paddingVertical: 10,
  },
  timeColon: { color: '#9ca3af', fontSize: 28, paddingHorizontal: 8 },
  modalBtnRow: { flexDirection: 'row', gap: 10 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 10, alignItems: 'center' },
  modalBtnGhost: { backgroundColor: 'transparent', borderWidth: 1, borderColor: '#2d2d3a' },
  modalBtnGhostText: { color: '#cbd5e1', fontWeight: '600' },
  modalBtnPrimary: { backgroundColor: '#3b82f6' },
  modalBtnPrimaryText: { color: '#ffffff', fontWeight: '700' },
});
