"""Export hasil ujian: txt / md (dibaca manusia) dan json (untuk dianalisis AI)."""
import json

MODE = {"normal": "Normal", "utbk": "UTBK (waktu per soal)"}

NOTE_FOR_AI = (
    "Ini hasil satu sesi ujian latihan pilihan ganda milik seorang siswa. Tugasmu: "
    "(1) analisis kelemahan berdasarkan akurasi per topik, soal yang dikerjakan lama (stuck), "
    "dan soal yang salah atau kosong; "
    "(2) tentukan topik yang harus diprioritaskan dan jelaskan alasannya pakai data; "
    "(3) buat rencana belajar bertahap yang konkret (urutan topik, jenis latihan, tips mengatur waktu per soal); "
    "(4) jawab dalam bahasa Indonesia yang mudah dipahami. "
    "Catatan: untuk soal yang tidak dijawab, kunci jawaban sengaja tidak disertakan."
)


def _sec(ms):
    return round((ms or 0) / 1000, 1)


def _dur(sec):
    sec = int(round(sec or 0))
    return f"{sec} dtk" if sec < 60 else f"{sec // 60} mnt {sec % 60} dtk"


def build_data(attempt, rows, an):
    exam = attempt.exam
    items = {i["number"]: i for i in an["items"]}
    total_ms = sum(a.time_spent_ms or 0 for a, _ in rows)
    n = len(rows)

    questions = []
    for a, q in rows:
        it = items[q.number]
        d = dict(
            number=q.number, topic=q.topic, type=q.type, text=q.text, options=q.options,
            status=it["status"], selected=a.selected or [], time_sec=_sec(it["ms"]),
            visits=it["visits"], stuck=it["stuck"], stuck_reasons=it["reasons"],
            priority_review=it["priority"],
        )
        if a.selected:  # soal tidak dijawab: kunci & penjelasan sengaja tidak dibuka
            d["correct"] = q.correct
            d["explanation"] = q.explanation
        questions.append(d)

    return dict(
        note_for_ai=NOTE_FOR_AI,
        exam=dict(title=exam.title, subject=exam.subject, mode=MODE.get(attempt.mode, attempt.mode),
                  seconds_per_question=exam.seconds_per_question, total_minutes=exam.total_minutes),
        attempt=dict(
            date_utc=attempt.started_at.strftime("%Y-%m-%d %H:%M"), score=attempt.score,
            correct=sum(1 for a, _ in rows if a.is_correct), total=n,
            duration_sec=int((attempt.finished_at - attempt.started_at).total_seconds()),
            active_time_sec=_sec(total_ms), avg_time_per_question_sec=_sec(total_ms / n if n else 0),
            time_tracking_available=an["has_time"],
        ),
        topics=[dict(topic=t["topic"], correct=t["correct"], total=t["n"], accuracy_pct=t["acc"],
                     avg_time_sec=_sec(t["ms"] / t["n"])) for t in an["topics"]],
        stuck_questions=[dict(number=i["number"], topic=i["topic"], time_sec=_sec(i["ms"]),
                              status=i["status"], reasons=i["reasons"], priority_review=i["priority"])
                         for i in an["stuck"]],
        questions=questions,
    )


def to_json(d):
    return json.dumps(d, ensure_ascii=False, indent=2)


def to_txt(d):
    e, a = d["exam"], d["attempt"]
    t_ok = a["time_tracking_available"]
    L = [f"HASIL UJIAN: {e['title']}", f"Mode: {e['mode']}", f"Tanggal (UTC): {a['date_utc']}", "",
         "RINGKASAN", f"- Skor: {a['score']} ({a['correct']}/{a['total']} benar)",
         f"- Durasi sesi: {_dur(a['duration_sec'])}"]
    if t_ok:
        L += [f"- Waktu aktif: {_dur(a['active_time_sec'])}",
              f"- Rata-rata per soal: {_dur(a['avg_time_per_question_sec'])}"]
    L += ["", "PER TOPIK (terlemah dulu)"]
    for t in d["topics"]:
        extra = f", rata-rata {_dur(t['avg_time_sec'])}" if t_ok else ""
        L.append(f"- {t['topic'] or 'Tanpa topik'}: {t['correct']}/{t['total']} benar ({t['accuracy_pct']}%){extra}")
    if d["stuck_questions"]:
        L += ["", "SOAL STUCK"]
        for s in d["stuck_questions"]:
            flag = " [PRIORITAS DIPELAJARI ULANG]" if s["priority_review"] else ""
            L.append(f"- No {s['number']} ({s['topic']}): {_dur(s['time_sec'])}, {s['status']}, "
                     f"{', '.join(s['reasons'])}{flag}")
    L += ["", "DETAIL PER SOAL", ""]
    for q in d["questions"]:
        time_part = f" {_dur(q['time_sec'])}, dikunjungi {q['visits']}x," if t_ok else ""
        L.append(f"{q['number']}. [{q['status'].upper()}]{time_part} topik: {q['topic'] or '-'}")
        L.append(f"   Soal: {q['text']}")
        if q["status"] == "kosong":
            L.append("   Tidak dijawab.")
        else:
            L.append(f"   Jawaban: {', '.join(q['selected'])} | Kunci: {', '.join(q['correct'])}")
            if q.get("explanation"):
                L.append(f"   Penjelasan: {q['explanation']}")
        L.append("")
    return "\n".join(L)


def _esc(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def to_md(d):
    e, a = d["exam"], d["attempt"]
    t_ok = a["time_tracking_available"]
    L = [f"# Hasil Ujian: {e['title']}", "",
         f"- **Mode:** {e['mode']}", f"- **Tanggal (UTC):** {a['date_utc']}",
         f"- **Skor:** {a['score']} ({a['correct']}/{a['total']} benar)",
         f"- **Durasi sesi:** {_dur(a['duration_sec'])}"]
    if t_ok:
        L += [f"- **Waktu aktif:** {_dur(a['active_time_sec'])}",
              f"- **Rata-rata per soal:** {_dur(a['avg_time_per_question_sec'])}"]
    L += ["", "## Akurasi per topik (terlemah dulu)", ""]
    L += ["| Topik | Benar | Akurasi | Rata-rata waktu |", "|---|---|---|---|"]
    for t in d["topics"]:
        avg = _dur(t["avg_time_sec"]) if t_ok else "-"
        L.append(f"| {_esc(t['topic'] or 'Tanpa topik')} | {t['correct']}/{t['total']} | {t['accuracy_pct']}% | {avg} |")
    if d["stuck_questions"]:
        L += ["", "## Soal stuck", ""]
        for s in d["stuck_questions"]:
            flag = " **(prioritas dipelajari ulang)**" if s["priority_review"] else ""
            L.append(f"- No {s['number']} ({s['topic']}): {_dur(s['time_sec'])}, {s['status']}, "
                     f"{', '.join(s['reasons'])}{flag}")
    L += ["", "## Detail per soal", ""]
    for q in d["questions"]:
        t = f" ({_dur(q['time_sec'])}, dikunjungi {q['visits']}x)" if t_ok else ""
        L += [f"### No {q['number']} - {q['status'].upper()}{t}", "",
              f"- Topik: {q['topic'] or '-'}", f"- Soal: {_esc(q['text'])}"]
        if q["status"] == "kosong":
            L.append("- Tidak dijawab.")
        else:
            L.append(f"- Jawaban: {', '.join(q['selected'])} | Kunci: {', '.join(q['correct'])}")
            if q.get("explanation"):
                L.append(f"- Penjelasan: {_esc(q['explanation'])}")
        L.append("")
    return "\n".join(L)
