package com.mori.babywidget.data

import android.content.Context
import android.content.SharedPreferences

class EventRepo private constructor(private val prefs: SharedPreferences) {

    fun lastTs(type: EventType): Long? {
        val v = prefs.getLong(keyLast(type), -1L)
        return if (v <= 0L) null else v
    }

    fun thresholdHours(type: EventType): Double {
        val bits = prefs.getLong(keyTh(type), java.lang.Double.doubleToRawLongBits(type.defaultThresholdHours))
        return java.lang.Double.longBitsToDouble(bits)
    }

    fun setLastTs(type: EventType, ts: Long) {
        prefs.edit().putLong(keyLast(type), ts).apply()
    }

    fun clearLast(type: EventType) {
        prefs.edit().remove(keyLast(type)).apply()
    }

    fun setThresholdHours(type: EventType, hours: Double) {
        prefs.edit().putLong(keyTh(type), java.lang.Double.doubleToRawLongBits(hours)).apply()
    }

    fun snapshot(): Map<EventType, Snapshot> = EventType.ALL.associateWith {
        Snapshot(lastTs = lastTs(it), thresholdHours = thresholdHours(it))
    }

    private fun keyLast(t: EventType) = "last_${t.key}"
    private fun keyTh(t: EventType) = "th_${t.key}"

    data class Snapshot(val lastTs: Long?, val thresholdHours: Double)

    companion object {
        private const val PREFS_NAME = "baby_widget_state"

        fun get(context: Context): EventRepo {
            val prefs = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            return EventRepo(prefs)
        }
    }
}
