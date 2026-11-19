from datetime import datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(10), default="user")  # user / admin
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(100), default="")
    total_minutes = db.Column(db.Integer, default=60)
    seconds_per_question = db.Column(db.Integer, default=90)  # dipakai fase 3
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    questions = db.relationship(
        "Question", backref="exam", order_by="Question.number",
        cascade="all, delete-orphan",
    )


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    type = db.Column(db.String(10), default="single")  # single / multiple
    topic = db.Column(db.String(200), default="")
    text = db.Column(db.Text, nullable=False)
    options = db.Column(db.JSON, nullable=False)   # {"A": "...", "B": "..."}
    correct = db.Column(db.JSON, nullable=False)   # ["A"] atau ["A", "C"]
    explanation = db.Column(db.Text, default="")


class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    room_id = db.Column(db.Integer, nullable=True)  # disiapkan buat battle (fase 6)
    mode = db.Column(db.String(10), default="normal")  # normal / utbk
    q_index = db.Column(db.Integer, default=1)          # soal aktif (mode utbk)
    q_started_at = db.Column(db.DateTime, nullable=True)  # kapan soal aktif dibuka (mode utbk)
    forfeited = db.Column(db.Boolean, default=False)       # gugur: keluar dari ujian UTBK
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)
    score = db.Column(db.Float, nullable=True)
    exam = db.relationship("Exam")
    answers = db.relationship("Answer", backref="attempt", cascade="all, delete-orphan")


class Answer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False)
    selected = db.Column(db.JSON, default=list)
    time_spent_ms = db.Column(db.Integer, default=0)  # diisi fase 2
    visit_count = db.Column(db.Integer, default=0)    # diisi fase 2
    is_correct = db.Column(db.Boolean, nullable=True)
    __table_args__ = (db.UniqueConstraint("attempt_id", "question_id"),)
