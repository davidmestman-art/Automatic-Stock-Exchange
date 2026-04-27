from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    # Alpaca API credentials (Fernet-encrypted at rest)
    alpaca_api_key_enc    = db.Column(db.Text, nullable=False, default="")
    alpaca_secret_key_enc = db.Column(db.Text, nullable=False, default="")
    alpaca_paper          = db.Column(db.Boolean, nullable=False, default=True)
    # Email notification settings
    notify_email                = db.Column(db.Text, nullable=False, default="")
    email_notifications_enabled = db.Column(db.Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"
