"""JWT authentication service: hashing, token creation, verification."""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from config import Settings, settings

ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 24

DEFAULT_SECRET_KEY = Settings.model_fields["secret_key"].default
DEFAULT_ADMIN_PASSWORD = Settings.model_fields["admin_password"].default


def secret_key_is_safe() -> bool:
    """False while SECRET_KEY is the public default (or trivially short).

    With the default key anyone can mint a valid admin cookie, so no token is
    issued or accepted until a real key is set in .env.
    """
    return settings.secret_key != DEFAULT_SECRET_KEY and len(settings.secret_key) >= 16


def env_password_is_default() -> bool:
    return settings.admin_password == DEFAULT_ADMIN_PASSWORD


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    if not secret_key_is_safe():
        raise RuntimeError("SECRET_KEY is the public default; refusing to issue tokens")
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=TOKEN_EXPIRE_HOURS))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    if not secret_key_is_safe():
        return None  # a token signed with the public default key proves nothing
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
