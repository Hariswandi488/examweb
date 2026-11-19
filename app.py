import json
import os
import re
from datetime import datetime, timedelta

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from werkzeug.security import check_password_hash, generate_password_hash

from analytics import analyze, fmt, progress_for_user
from config import BASE_DIR, Config
from exporter import build_data, to_json, to_md, to_txt
from importer import import_exam
from models import Answer, Attempt, Exam, Question, User, db

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Login dulu ya."
app.jinja_env.filters["dur"] = fmt

GRACE_SECONDS = 15  # toleransi submit terakhir saat waktu habis


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.after_request
def no_cache_attempt_pages(resp):
    """Halaman jangan disimpan browser, supaya tombol back selalu minta ulang ke server."""
    if request.endpoint != "static":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


NEW_COLUMNS = {
    "attempt": {
        "q_index": "INTEGER DEFAULT 1",
        "q_started_at": "DATETIME",
        "forfeited": "BOOLEAN DEFAULT 0",
    },
    "answer": {
        "flagged": "BOOLEAN DEFAULT 0",
    },
}


def ensure_columns():
    """Migrasi ringan SQLite: tambah kolom baru kalau DB lama belum punya."""
    with db.engine.begin() as conn:
        for table, columns in NEW_COLUMNS.items():
            have = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in have:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


with app.app_context():
    db.create_all()
    ensure_columns()


# ---------- helper ----------
def deadline_of(attempt):
    exam = attempt.exam
    if attempt.mode == "utbk":
        # akhir timeline = awal soal aktif + sisa soal x jatah waktu per soal
        left = max(0, len(exam.questions) - (attempt.q_index or 1) + 1)
        return attempt.q_started_at + timedelta(seconds=left * exam.seconds_per_question)
    return attempt.started_at + timedelta(minutes=exam.total_minutes)


def own_attempt_or_404(aid):
    attempt = db.session.get(Attempt, aid)
    if not attempt or attempt.user_id != current_user.id:
        abort(404)
    return attempt


def finish_attempt(attempt):
    if attempt.finished_at:
        return
    correct = 0
    for a in attempt.answers:
        q = db.session.get(Question, a.question_id)
        a.is_correct = bool(a.selected) and set(a.selected) == set(q.correct)
        correct += 1 if a.is_correct else 0
    total = len(attempt.answers)
    attempt.score = round(correct / total * 100, 1) if total else 0
    # kalau telat submit, waktu selesai dipatok ke deadline
    attempt.finished_at = min(datetime.utcnow(), deadline_of(attempt))
    db.session.commit()


def forfeit_attempt(attempt):
    """Gugur: skor 0, jawaban dianggap salah semua."""
    if attempt.finished_at:
        return
    for ans in attempt.answers:
        ans.is_correct = False
    attempt.score = 0.0
    attempt.forfeited = True
    attempt.finished_at = min(datetime.utcnow(), deadline_of(attempt))
    db.session.commit()


@app.before_request
def utbk_leave_guard():
    """Keluar dari halaman ujian UTBK (home, tab lain, back, dll) = otomatis gugur."""
    if not current_user.is_authenticated or request.endpoint in (None, "static"):
        return
    active = Attempt.query.filter_by(user_id=current_user.id, mode="utbk", finished_at=None).all()
    left = False
    for at in active:
        if request.endpoint in ("attempt_page", "autosave") and (request.view_args or {}).get("aid") == at.id:
            continue
        if deadline_of(at) <= datetime.utcnow():
            finish_attempt(at)       # waktu sudah habis semua: dinilai normal
        else:
            forfeit_attempt(at)
            left = True
    if left:
        flash("Kamu keluar dari halaman ujian UTBK, jadi ujian dinyatakan GUGUR (skor 0).", "error")


# ---------- auth ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(username) < 3 or len(password) < 6:
            flash("Username min 3 karakter, password min 6 karakter.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Username sudah dipakai.", "error")
        else:
            role = "admin" if User.query.count() == 0 else "user"  # user pertama = admin
            user = User(username=username, role=role,
                        password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
        if user and check_password_hash(user.password_hash, request.form.get("password", "")):
            login_user(user)
            return redirect(url_for("index"))
        flash("Username atau password salah.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("login"))


# ---------- halaman utama ----------
@app.route("/")
@login_required
def index():
    exams = (Exam.query
             .filter((Exam.is_public == True) | (Exam.owner_id == current_user.id))  # noqa: E712
             .order_by(Exam.created_at.desc()).all())
    attempts = (Attempt.query.filter_by(user_id=current_user.id)
                .order_by(Attempt.started_at.desc()).limit(20).all())
    return render_template("index.html", exams=exams, attempts=attempts)


def parse_json_loose(raw):
    """Ambil blok JSON dari teks (buang code fence / kata pembuka AI). Return (data, error)."""
    raw = raw.replace("\ufeff", "")
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None, "Nggak ketemu JSON di teks itu. Pastikan yang di-paste adalah jawaban AI yang berisi kode JSON ({ ... })."
    try:
        return json.loads(raw[start:end + 1]), None
    except json.JSONDecodeError as e:
        return None, (f"JSON rusak di baris {e.lineno}, kolom {e.colno}: {e.msg}. "
                      "Kirim JSON ini ke AI dan minta diperbaiki.")


@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "GET":
        return render_template("upload.html", pasted="")

    pasted = request.form.get("pasted", "")
    f = request.files.get("file")

    def back(msg):
        flash(msg, "error")
        return render_template("upload.html", pasted=pasted)

    if f and f.filename:  # file diprioritaskan kalau dua-duanya diisi
        if not f.filename.lower().endswith(".json"):
            return back("File harus berekstensi .json.")
        try:
            raw = f.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return back("File tidak bisa dibaca (bukan teks UTF-8).")
    elif pasted.strip():
        raw = pasted
    else:
        return back("Paste JSON dari AI atau pilih file .json dulu.")

    data, err = parse_json_loose(raw)
    if err:
        return back(err)

    wanted = ""
    if isinstance(data.get("exam"), dict):
        wanted = str(data["exam"].get("title", "")).strip()[:200]
    exam, errors = import_exam(data, current_user.id, request.form.get("is_public") == "1")
    if errors:
        for e in errors[:20]:
            flash(e, "error")
        if len(errors) > 20:
            flash(f"...dan {len(errors) - 20} error lain.", "error")
        return render_template("upload.html", pasted=pasted)

    if wanted and exam.title != wanted:
        flash(f"Judul '{wanted}' sudah ada, jadi dinamai '{exam.title}'.", "ok")
    flash(f"Berhasil upload '{exam.title}' ({len(exam.questions)} soal).", "ok")
    return redirect(url_for("index"))


@app.route("/exam/<int:eid>/delete", methods=["POST"])
@login_required
def delete_exam(eid):
    exam = db.session.get(Exam, eid)
    if not exam:
        abort(404)
    if exam.owner_id != current_user.id and current_user.role != "admin":
        abort(403)
    for at in Attempt.query.filter_by(exam_id=eid).all():
        db.session.delete(at)
    db.session.delete(exam)
    db.session.commit()
    flash("Ujian dihapus.", "ok")
    return redirect(url_for("index"))


# ---------- ujian (mode normal) ----------
@app.route("/exam/<int:eid>/begin", methods=["POST"])
@login_required
def begin(eid):
    exam = db.session.get(Exam, eid)
    if not exam or not (exam.is_public or exam.owner_id == current_user.id):
        abort(404)
    mode = request.form.get("mode", "normal")
    if mode not in ("normal", "utbk"):
        mode = "normal"
    attempt = Attempt(exam_id=exam.id, user_id=current_user.id, mode=mode,
                      q_index=1, q_started_at=datetime.utcnow())
    db.session.add(attempt)
    db.session.flush()
    for q in exam.questions:
        db.session.add(Answer(attempt_id=attempt.id, question_id=q.id, selected=[]))
    db.session.commit()
    return redirect(url_for("attempt_page", aid=attempt.id, q=1))


def sync_utbk(attempt, answers):
    """Majukan soal otomatis kalau jatah waktunya habis (server = sumber kebenaran).
    Return sisa detik soal aktif, atau None kalau semua soal sudah lewat."""
    exam = attempt.exam
    questions = exam.questions
    n, S = len(questions), exam.seconds_per_question
    now = datetime.utcnow()
    elapsed = (now - attempt.q_started_at).total_seconds()
    skipped = min(int(elapsed // S), n - attempt.q_index + 1)
    if skipped > 0:
        for k in range(attempt.q_index, attempt.q_index + skipped):
            ans = answers[questions[k - 1].id]
            ans.time_spent_ms = (ans.time_spent_ms or 0) + S * 1000
            ans.visit_count = (ans.visit_count or 0) + 1
        attempt.q_index += skipped
        attempt.q_started_at += timedelta(seconds=skipped * S)
        db.session.commit()
    if attempt.q_index > n:
        return None
    return max(1, int(S - (now - attempt.q_started_at).total_seconds()))


def utbk_page(attempt):
    exam = attempt.exam
    questions = exam.questions
    n, S = len(questions), exam.seconds_per_question
    answers = {a.question_id: a for a in attempt.answers}
    aid = attempt.id

    if request.method == "POST":
        now = datetime.utcnow()
        cur = attempt.q_index
        posted = request.form.get("q", 0, type=int)
        expiry = attempt.q_started_at + timedelta(seconds=S)
        # hanya jawaban untuk soal aktif (dan belum lewat toleransi) yang diterima
        if cur <= n and posted == cur and now <= expiry + timedelta(seconds=GRACE_SECONDS):
            question = questions[cur - 1]
            selected = [c for c in request.form.getlist("choice") if c in question.options]
            if question.type == "single":
                selected = selected[:1]
            ans = answers[question.id]
            ans.selected = sorted(set(selected))
            spent = min((now - attempt.q_started_at).total_seconds(), S)  # waktu diukur server
            ans.time_spent_ms = (ans.time_spent_ms or 0) + int(spent * 1000)
            ans.visit_count = (ans.visit_count or 0) + 1
            if cur >= n:
                db.session.commit()
                finish_attempt(attempt)
                return redirect(url_for("result", aid=aid))
            attempt.q_index = cur + 1
            attempt.q_started_at = min(now, expiry)
            db.session.commit()
        return redirect(url_for("attempt_page", aid=aid))

    remaining = sync_utbk(attempt, answers)
    if remaining is None:
        finish_attempt(attempt)
        return redirect(url_for("result", aid=aid))
    q = attempt.q_index
    question = questions[q - 1]
    return render_template(
        "exam_utbk.html", exam=exam, question=question, q=q, total=n,
        options=sorted(question.options.items()), selected=answers[question.id].selected or [],
        remaining=remaining, aid=aid,
    )


@app.route("/attempt/<int:aid>/autosave", methods=["POST"])
@login_required
def autosave(aid):
    """Simpan jawaban sementara (dipanggil otomatis dari browser), supaya refresh nggak menghilangkan pilihan."""
    attempt = own_attempt_or_404(aid)
    if attempt.finished_at:
        return "", 204
    data = request.get_json(silent=True) or {}
    exam = attempt.exam
    questions = exam.questions
    q = data.get("q")
    if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= len(questions):
        return "", 204
    question = questions[q - 1]
    choice = data.get("choice", [])
    if not isinstance(choice, list):
        return "", 204
    selected = [c for c in choice if isinstance(c, str) and c in question.options]
    if question.type == "single":
        selected = selected[:1]
    selected = sorted(set(selected))
    now = datetime.utcnow()
    ans = Answer.query.filter_by(attempt_id=attempt.id, question_id=question.id).one()

    if attempt.mode == "utbk":
        expiry = attempt.q_started_at + timedelta(seconds=exam.seconds_per_question)
        if q != attempt.q_index or now > expiry:
            return "", 204
        ans.selected = selected
    else:
        if (deadline_of(attempt) - now).total_seconds() < -GRACE_SECONDS:
            return "", 204
        elapsed = data.get("elapsed_ms", 0)
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool):
            elapsed = 0
        elapsed = max(0, min(int(elapsed), exam.total_minutes * 60_000))
        ans.selected = selected
        ans.flagged = bool(data.get("flagged"))
        ans.time_spent_ms = (ans.time_spent_ms or 0) + elapsed
    db.session.commit()
    return "", 204


@app.route("/attempt/<int:aid>", methods=["GET", "POST"])
@login_required
def attempt_page(aid):
    attempt = own_attempt_or_404(aid)
    if attempt.finished_at:
        return redirect(url_for("result", aid=aid))
    if attempt.mode == "utbk":
        return utbk_page(attempt)

    exam = attempt.exam
    questions = exam.questions
    total = len(questions)
    remaining = int((deadline_of(attempt) - datetime.utcnow()).total_seconds())
    # waktu habis: GET langsung selesai; POST masih diterima sebentar (jawaban terakhir ikut tersimpan)
    if remaining <= 0 and (request.method == "GET" or remaining < -GRACE_SECONDS):
        finish_attempt(attempt)
        return redirect(url_for("result", aid=aid))
    answers = {a.question_id: a for a in attempt.answers}

    if request.method == "POST":
        q = max(1, min(total, request.form.get("q", 1, type=int)))
        question = questions[q - 1]
        selected = [c for c in request.form.getlist("choice") if c in question.options]
        if question.type == "single":
            selected = selected[:1]
        ans = answers[question.id]
        ans.selected = sorted(set(selected))
        ans.flagged = request.form.get("flag") == "1"
        # waktu di soal ini (ms), dikirim browser; dibatasi biar nggak bisa ngawur
        elapsed = request.form.get("elapsed_ms", 0, type=int) or 0
        elapsed = max(0, min(elapsed, exam.total_minutes * 60_000))
        ans.time_spent_ms = (ans.time_spent_ms or 0) + elapsed
        ans.visit_count = (ans.visit_count or 0) + 1
        db.session.commit()

        action = request.form.get("action", "")
        if action == "finish" or remaining <= 0:
            finish_attempt(attempt)
            return redirect(url_for("result", aid=aid))
        if action == "next":
            q += 1
        elif action == "prev":
            q -= 1
        elif action.startswith("goto:"):
            try:
                q = int(action[5:])
            except ValueError:
                pass
        return redirect(url_for("attempt_page", aid=aid, q=max(1, min(total, q))))

    q = max(1, min(total, request.args.get("q", 1, type=int)))
    question = questions[q - 1]
    answered = [i for i, qq in enumerate(questions, 1) if answers[qq.id].selected]
    flagged = [i for i, qq in enumerate(questions, 1) if answers[qq.id].flagged]
    return render_template(
        "exam.html", exam=exam, question=question, q=q, total=total,
        attempt=attempt, flagged=flagged, is_flagged=bool(answers[question.id].flagged),
        options=sorted(question.options.items()),
        selected=answers[question.id].selected or [],
        answered=answered, remaining=remaining,
    )


@app.route("/progress")
@login_required
def progress():
    return render_template("progress.html", groups=progress_for_user(current_user.id))


def result_rows(attempt):
    answers = {a.question_id: a for a in attempt.answers}
    return [(answers[q.id], q) for q in attempt.exam.questions]


@app.route("/result/<int:aid>")
@login_required
def result(aid):
    attempt = own_attempt_or_404(aid)
    if not attempt.finished_at:
        return redirect(url_for("attempt_page", aid=aid))
    if attempt.forfeited:
        return render_template("result_forfeit.html", attempt=attempt)
    rows = result_rows(attempt)
    correct = sum(1 for a, _ in rows if a.is_correct)
    secs = int((attempt.finished_at - attempt.started_at).total_seconds())
    return render_template(
        "result.html", attempt=attempt, rows=rows, correct=correct,
        total=len(rows), duration=f"{secs // 60} menit {secs % 60} detik",
        analysis=analyze(attempt, rows),
    )


@app.route("/result/<int:aid>/export")
@login_required
def export(aid):
    attempt = own_attempt_or_404(aid)
    if not attempt.finished_at:
        return redirect(url_for("attempt_page", aid=aid))
    if attempt.forfeited:
        return redirect(url_for("result", aid=aid))
    kind = request.args.get("format", "txt")
    if kind not in ("txt", "md", "json"):
        abort(400)
    rows = result_rows(attempt)
    data = build_data(attempt, rows, analyze(attempt, rows))
    body = {"txt": to_txt, "md": to_md, "json": to_json}[kind](data)
    mime = {"txt": "text/plain", "md": "text/markdown", "json": "application/json"}[kind]
    slug = re.sub(r"[^a-z0-9]+", "-", attempt.exam.title.lower()).strip("-")[:40] or "hasil"
    resp = Response(body, mimetype=mime + "; charset=utf-8")
    resp.headers["Content-Disposition"] = f'attachment; filename="hasil-{slug}-{attempt.id}.{kind}"'
    return resp


if __name__ == "__main__":
    app.run(debug=True)
