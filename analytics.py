"""Analisis waktu & performa per percobaan (fase 2)."""


def fmt(ms):
    s = int(round((ms or 0) / 1000))
    return f"{s} dtk" if s < 60 else f"{s // 60} mnt {s % 60} dtk"


def _status(a):
    if a.is_correct:
        return "benar"
    return "salah" if a.selected else "kosong"


def analyze(attempt, rows):
    """rows = [(Answer, Question), ...] berurutan sesuai nomor soal."""
    exam = attempt.exam
    n = len(rows)
    times = [a.time_spent_ms or 0 for a, _ in rows]
    total_ms = sum(times)
    visited = [t for t in times if t > 0]
    avg_visited = total_ms / len(visited) if visited else 0
    has_time = total_ms > 0
    limit_ms = (exam.seconds_per_question or 90) * 1000

    items = []
    for a, q in rows:
        t = a.time_spent_ms or 0
        status = _status(a)
        reasons = []
        if has_time:
            if t >= 20000 and t > 2 * avg_visited:
                reasons.append("lebih dari 2x rata-rata")
            if attempt.mode == "utbk":
                if t >= limit_ms - 1000:
                    reasons.append("waktu soal habis")
            elif t > limit_ms:
                reasons.append(f"melewati {exam.seconds_per_question} dtk")
        items.append(dict(
            number=q.number, topic=q.topic or "Tanpa topik", ms=t, time=fmt(t),
            status=status, visits=a.visit_count or 0, stuck=bool(reasons),
            reasons=reasons, priority=bool(reasons) and status != "benar",
        ))

    top5 = sorted([i for i in items if i["ms"] > 0], key=lambda i: -i["ms"])[:5]
    stuck = sorted([i for i in items if i["stuck"]], key=lambda i: (not i["priority"], -i["ms"]))

    topics = {}
    for it in items:
        d = topics.setdefault(it["topic"], dict(topic=it["topic"], n=0, correct=0, ms=0))
        d["n"] += 1
        d["correct"] += 1 if it["status"] == "benar" else 0
        d["ms"] += it["ms"]
    for d in topics.values():
        d["acc"] = round(d["correct"] / d["n"] * 100)
        d["avg"] = fmt(d["ms"] / d["n"])
        d["total"] = fmt(d["ms"])
    topic_rows = sorted(topics.values(), key=lambda d: (d["acc"], -d["ms"] / d["n"]))

    return dict(
        has_time=has_time, total_active=fmt(total_ms), avg=fmt(total_ms / n if n else 0),
        items=items, top5=top5, stuck=stuck, topics=topic_rows, seconds_limit=exam.seconds_per_question,
    )
