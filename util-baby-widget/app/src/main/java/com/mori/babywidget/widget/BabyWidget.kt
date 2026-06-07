package com.mori.babywidget.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.GlanceTheme
import androidx.glance.action.ActionParameters
import androidx.glance.action.actionParametersOf
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.cornerRadius
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Box
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import com.mori.babywidget.data.EventRepo
import com.mori.babywidget.data.EventType
import com.mori.babywidget.data.Formatting
import com.mori.babywidget.data.Level
import com.mori.babywidget.data.LevelCalc

private val BgRoot = Color(0xFF0B0B0F)
private val CardSafe = Color(0xFF1F1F28)
private val CardWarn = Color(0xFF3A2F12)
private val CardDanger = Color(0xFF3A1414)
private val TextPrimary = Color(0xFFE5E7EB)
private val TextMuted = Color(0xFF9CA3AF)
private val TextWarn = Color(0xFFFBBF24)
private val TextDanger = Color(0xFFFECACA)

private fun cp(color: Color): ColorProvider = ColorProvider(color)

class BabyWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        provideContent {
            GlanceTheme { BabyWidgetContent(context) }
        }
    }
}

@Composable
private fun BabyWidgetContent(context: Context) {
    val repo = EventRepo.get(context)
    val snapshot = repo.snapshot()
    val now = System.currentTimeMillis()

    Column(
        modifier = GlanceModifier
            .fillMaxSize()
            .background(BgRoot)
            .cornerRadius(20.dp)
            .padding(8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        EventType.ALL.forEachIndexed { idx, type ->
            val snap = snapshot[type] ?: return@forEachIndexed
            val level = LevelCalc.compute(snap.lastTs, snap.thresholdHours, now)
            EventRow(type = type, lastTs = snap.lastTs, level = level, now = now)
            if (idx != EventType.ALL.lastIndex) Spacer(GlanceModifier.height(6.dp))
        }
    }
}

@Composable
private fun EventRow(type: EventType, lastTs: Long?, level: Level, now: Long) {
    val bg = when (level) {
        Level.DANGER -> CardDanger
        Level.WARN -> CardWarn
        else -> CardSafe
    }
    val elapsedColor = when (level) {
        Level.DANGER -> TextDanger
        Level.WARN -> TextWarn
        else -> TextPrimary
    }
    Row(
        modifier = GlanceModifier
            .fillMaxWidth()
            .background(bg)
            .cornerRadius(12.dp)
            .padding(horizontal = 10.dp, vertical = 8.dp)
            .clickable(
                onClick = actionRunCallback<TapAction>(
                    parameters = actionParametersOf(TapAction.TYPE_KEY to type.key),
                ),
            ),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(text = type.emoji, style = TextStyle(fontSize = 22.sp, color = cp(TextPrimary)))
        Spacer(GlanceModifier.width(8.dp))
        Column(modifier = GlanceModifier.defaultWeight()) {
            Text(
                text = type.label,
                style = TextStyle(fontSize = 12.sp, fontWeight = FontWeight.Medium, color = cp(TextMuted)),
            )
            Text(
                text = Formatting.elapsed(lastTs, now),
                style = TextStyle(fontSize = 22.sp, fontWeight = FontWeight.Bold, color = cp(elapsedColor)),
            )
        }
        Box(contentAlignment = Alignment.CenterEnd) {
            Text(
                text = lastTs?.let { Formatting.clock(it) } ?: "—",
                style = TextStyle(fontSize = 11.sp, color = cp(TextMuted)),
            )
        }
    }
}

class TapAction : ActionCallback {
    override suspend fun onAction(
        context: Context,
        glanceId: GlanceId,
        parameters: ActionParameters,
    ) {
        val typeKey = parameters[TYPE_KEY] ?: return
        val type = EventType.ALL.firstOrNull { it.key == typeKey } ?: return
        EventRepo.get(context).setLastTs(type, System.currentTimeMillis())
        BabyWidget().update(context, glanceId)
    }

    companion object {
        val TYPE_KEY: ActionParameters.Key<String> = ActionParameters.Key("type")
    }
}
