package com.mori.babywidget.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.mori.babywidget.data.EventRepo
import com.mori.babywidget.data.EventType
import com.mori.babywidget.data.Formatting
import com.mori.babywidget.data.Level
import com.mori.babywidget.data.LevelCalc
import com.mori.babywidget.widget.WidgetSync
import kotlinx.coroutines.delay

private val BgRoot = Color(0xFF0B0B0F)
private val CardSafe = Color(0xFF16161D)
private val CardWarn = Color(0xFF3A2F12)
private val CardDanger = Color(0xFF3A1414)
private val BorderSafe = Color(0xFF26262F)
private val BorderWarn = Color(0xFF7A6322)
private val BorderDanger = Color(0xFF7A2A2A)
private val TextPrimary = Color(0xFFE5E7EB)
private val TextMuted = Color(0xFF9CA3AF)
private val TextWarn = Color(0xFFFBBF24)
private val TextDanger = Color(0xFFFECACA)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen() {
    val context = LocalContext.current
    val repo = remember { EventRepo.get(context) }
    var version by remember { mutableIntStateOf(0) }
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    var settingsOpen by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        while (true) {
            now = System.currentTimeMillis()
            delay(30_000L)
        }
    }

    val snapshot = remember(version, now) { repo.snapshot() }

    MaterialTheme(colorScheme = darkColorScheme(background = BgRoot, surface = BgRoot)) {
        Surface(color = BgRoot, modifier = Modifier.fillMaxSize()) {
            Column(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text("Baby Widget", color = TextMuted, fontWeight = FontWeight.SemiBold)
                    Text(Formatting.clock(now), color = TextMuted)
                }

                EventType.ALL.forEach { type ->
                    val snap = snapshot[type] ?: return@forEach
                    EventCard(
                        type = type,
                        lastTs = snap.lastTs,
                        thresholdHours = snap.thresholdHours,
                        now = now,
                        onTap = {
                            repo.setLastTs(type, System.currentTimeMillis())
                            WidgetSync.requestUpdate(context)
                            version++
                        },
                        onUndo = {
                            repo.clearLast(type)
                            WidgetSync.requestUpdate(context)
                            version++
                        },
                    )
                }

                Spacer(Modifier.weight(1f))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    TextButton(onClick = { settingsOpen = true }) {
                        Text("⚙ 임계치 설정", color = TextMuted)
                    }
                    TextButton(onClick = {
                        EventType.ALL.forEach { repo.clearLast(it); repo.setThresholdHours(it, it.defaultThresholdHours) }
                        WidgetSync.requestUpdate(context)
                        version++
                    }) {
                        Text("↺ 초기화", color = TextMuted)
                    }
                }
            }
        }
    }

    if (settingsOpen) {
        ThresholdsDialog(
            repo = repo,
            onDismiss = { settingsOpen = false },
            onSaved = {
                settingsOpen = false
                WidgetSync.requestUpdate(context)
                version++
            },
        )
    }
}

@Composable
private fun EventCard(
    type: EventType,
    lastTs: Long?,
    thresholdHours: Double,
    now: Long,
    onTap: () -> Unit,
    onUndo: () -> Unit,
) {
    val level = LevelCalc.compute(lastTs, thresholdHours, now)
    val bg = when (level) {
        Level.DANGER -> CardDanger
        Level.WARN -> CardWarn
        else -> CardSafe
    }
    val border = when (level) {
        Level.DANGER -> BorderDanger
        Level.WARN -> BorderWarn
        else -> BorderSafe
    }
    val elapsedColor = when (level) {
        Level.DANGER -> TextDanger
        Level.WARN -> TextWarn
        else -> TextPrimary
    }
    val meta = if (lastTs != null) "마지막 ${Formatting.clock(lastTs)} · 임계 ${thresholdHours}h" else "기록 없음 · 임계 ${thresholdHours}h"

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(16.dp))
            .background(bg)
            .border(1.dp, border, RoundedCornerShape(16.dp))
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(type.emoji, fontSize = 34.sp)
        Spacer(Modifier.width(14.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(type.label, color = TextPrimary, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
            Text(Formatting.elapsed(lastTs, now), color = elapsedColor, fontSize = 28.sp, fontWeight = FontWeight.Bold)
            Text(meta, color = TextMuted, fontSize = 12.sp)
        }
        Column(horizontalAlignment = Alignment.End, verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Button(onClick = onTap) { Text("탭") }
            TextButton(onClick = onUndo) { Text("↶", color = TextMuted) }
        }
    }
}

@Composable
private fun ThresholdsDialog(
    repo: EventRepo,
    onDismiss: () -> Unit,
    onSaved: () -> Unit,
) {
    val initial = remember { EventType.ALL.associateWith { repo.thresholdHours(it).toString() } }
    val state = remember { mutableStateMapOf<EventType, String>().apply { putAll(initial) } }
    var error by rememberSaveable { mutableStateOf<String?>(null) }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(onClick = {
                val parsed = state.mapValues { (_, v) -> v.toDoubleOrNull() }
                if (parsed.any { (_, v) -> v == null || v <= 0.0 }) {
                    error = "0보다 큰 숫자만 입력"
                    return@TextButton
                }
                parsed.forEach { (k, v) -> repo.setThresholdHours(k, v!!) }
                onSaved()
            }) { Text("저장") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("취소") } },
        title = { Text("임계치 (시간 단위)") },
        text = {
            Column {
                EventType.ALL.forEach { type ->
                    OutlinedTextField(
                        value = state[type] ?: "",
                        onValueChange = { state[type] = it; error = null },
                        label = { Text("${type.emoji} ${type.label}") },
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = KeyboardType.Decimal),
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                    )
                }
                if (error != null) {
                    Text(error!!, color = TextDanger, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))
                }
                Text(
                    "임계치 1시간 전 = 노랑, 30분 전/초과 = 빨강",
                    color = TextMuted,
                    fontSize = 12.sp,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        },
    )
}
