import json
import os
from datetime import datetime, timedelta

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from werkzeug.security import check_password_hash, generate_password_hash

from config import BASE_DIR, Config
from importer import import_exam
from models import Answer, Attempt, Exam, Question, User, db

app = Flask(__name__)
app.config.from_object(Config)
os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Login dulu ya."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()


# ---------- helper ----------
def deadline_of(attempt):
    return attempt.started_at + timedelta(minutes=attempt.exam.total_minutes)


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
    attempt = Attempt(exam_id=exam.id, user_id=current_user.id, mode="normal")
    db.session.add(attempt)
    db.session.flush()
    for q in exam.questions:
        db.session.add(Answer(attempt_id=attempt.id, question_id=q.id, selected=[]))
    db.session.commit()
    return redirect(url_for("attempt_page", aid=attempt.id, q=1))


@app.route("/attempt/<int:aid>", methods=["GET", "POST"])
@login_required
def attempt_page(aid):
    attempt = own_attempt_or_404(aid)
    if attempt.finished_at:
        return redirect(url_for("result", aid=aid))

    exam = attempt.exam
    questions = exam.questions
    total = len(questions)
    remaining = int((deadline_of(attempt) - datetime.utcnow()).total_seconds())
    if remaining <= 0:
        finish_attempt(attempt)
        return redirect(url_for("result", aid=aid))
    answers = {a.question_id: a for a in attempt.answers}

    if request.method == "POST":
        q = max(1, min(total, request.form.get("q", 1, type=int)))
        question = questions[q - 1]
        selected = [c for c in request.form.getlist("choice") if c in question.options]
        if question.type == "single":
            selected = selected[:1]
        answers[question.id].selected = sorted(set(selected))
        db.session.commit()

        action = request.form.get("action", "")
        if action == "finish":
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
    return render_template(
        "exam.html", exam=exam, question=question, q=q, total=total,
        options=sorted(question.options.items()),
        selected=answers[question.id].selected or [],
        answered=answered, remaining=remaining,
    )


@app.route("/result/<int:aid>")
@login_required
def result(aid):
    attempt = own_attempt_or_404(aid)
    if not attempt.finished_at:
        return redirect(url_for("attempt_page", aid=aid))
    answers = {a.question_id: a for a in attempt.answers}
    rows = [(answers[q.id], q) for q in attempt.exam.questions]
    correct = sum(1 for a, _ in rows if a.is_correct)
    secs = int((attempt.finished_at - attempt.started_at).total_seconds())
    return render_template(
        "result.html", attempt=attempt, rows=rows, correct=correct,
        total=len(rows), duration=f"{secs // 60} menit {secs % 60} detik",
    )


if __name__ == "__main__":
    app.run(debug=True)
