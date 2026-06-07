package com.mori.babywidget.data

object Formatting {
    fun elapsed(lastTs: Long?, now: Long = System.currentTimeMillis()): String {
        if (lastTs == null) return "—"
        val totalMin = ((now - lastTs).coerceAtLeast(0L) / 60_000L).toInt()
        val h = totalMin / 60
        val m = totalMin % 60
        return "%d:%02d".format(h, m)
    }

    fun clock(ts: Long): String {
        val c = java.util.Calendar.getInstance().apply { timeInMillis = ts }
        return "%02d:%02d".format(c.get(java.util.Calendar.HOUR_OF_DAY), c.get(java.util.Calendar.MINUTE))
    }
}
