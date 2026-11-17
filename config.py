import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # Di PythonAnywhere: set SECRET_KEY lewat environment / ganti string ini
    SECRET_KEY = os.environ.get("SECRET_KEY", "ganti-ini-sebelum-deploy")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "instance", "exam.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # batas upload 2 MB
