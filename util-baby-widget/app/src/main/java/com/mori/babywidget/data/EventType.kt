package com.mori.babywidget.data

enum class EventType(val key: String, val label: String, val emoji: String, val defaultThresholdHours: Double) {
    FEED("feed", "밥", "🍼", 4.0),
    SLEEP("sleep", "잠", "😴", 3.5),
    LOTION("lotion", "로션", "🧴", 12.0),
    ;

    companion object {
        val ALL: List<EventType> = values().toList()
    }
}

enum class Level { NONE, SAFE, WARN, DANGER }

object LevelCalc {
    private const val WARN_REMAINING_MS = 60L * 60_000L
    private const val DANGER_REMAINING_MS = 30L * 60_000L

    fun compute(lastTs: Long?, thresholdHours: Double, now: Long = System.currentTimeMillis()): Level {
        if (lastTs == null) return Level.NONE
        val elapsed = now - lastTs
        val threshold = (thresholdHours * 3_600_000.0).toLong()
        val remaining = threshold - elapsed
        return when {
            remaining <= DANGER_REMAINING_MS -> Level.DANGER
            remaining <= WARN_REMAINING_MS -> Level.WARN
            else -> Level.SAFE
        }
    }
}
