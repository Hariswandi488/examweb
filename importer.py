from models import Exam, Question, db


def _to_int(value, default, minimum, maximum):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, n))


def import_exam(data, owner_id, is_public=True):
    """Validasi JSON soal lalu simpan. Return (exam, errors)."""
    if not isinstance(data, dict):
        return None, ["Isi file harus object JSON dengan kunci 'exam' dan 'questions'."]

    errors = []
    info = data.get("exam")
    qs = data.get("questions")
    if not isinstance(info, dict) or not str(info.get("title", "")).strip():
        errors.append("'exam.title' wajib diisi.")
    if not isinstance(qs, list) or not qs:
        errors.append("'questions' harus list dan tidak boleh kosong.")
    if errors:
        return None, errors

    parsed, seen = [], set()
    for idx, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            errors.append(f"Soal urutan ke-{idx}: harus berupa object.")
            continue
        number = q.get("number", idx)
        if not isinstance(number, int) or isinstance(number, bool):
            errors.append(f"Soal urutan ke-{idx}: 'number' harus angka bulat.")
            continue
        tag = f"Soal no {number}"
        local = []
        if number in seen:
            local.append("nomor soal dobel.")
        seen.add(number)

        qtype = q.get("type", "single")
        text = str(q.get("text", "")).strip()
        options = q.get("options")
        correct = q.get("correct")

        if qtype not in ("single", "multiple"):
            local.append("'type' harus 'single' atau 'multiple'.")
        if not text:
            local.append("'text' kosong.")
        if (not isinstance(options, dict) or len(options) < 2
                or any(not str(v).strip() for v in options.values())):
            local.append("'options' harus object, minimal 2 pilihan, tidak boleh ada yang kosong.")
            options = None
        if not isinstance(correct, list) or not correct:
            local.append("'correct' harus list berisi minimal 1 kunci.")
        elif options is not None:
            bad = [c for c in correct if not isinstance(c, str) or c not in options]
            if bad:
                local.append(f"jawaban benar {bad} tidak ada di 'options'.")
            elif qtype == "single" and len(set(correct)) != 1:
                local.append("tipe 'single' harus tepat 1 jawaban benar.")

        errors += [f"{tag}: {m}" for m in local]
        if not local:
            parsed.append(dict(
                number=number, type=qtype, text=text,
                topic=str(q.get("topic", "")).strip()[:200],
                options={k: str(v).strip() for k, v in options.items()},
                correct=sorted(set(correct)),
                explanation=str(q.get("explanation", "")).strip(),
            ))

    if errors:
        return None, errors

    exam = Exam(
        title=str(info["title"]).strip()[:200],
        subject=str(info.get("subject", "")).strip()[:100],
        total_minutes=_to_int(info.get("total_minutes"), 60, 1, 600),
        seconds_per_question=_to_int(info.get("seconds_per_question"), 90, 10, 3600),
        owner_id=owner_id,
        is_public=is_public,
    )
    db.session.add(exam)
    db.session.flush()
    for p in sorted(parsed, key=lambda x: x["number"]):
        db.session.add(Question(exam_id=exam.id, **p))
    db.session.commit()
    return exam, []
