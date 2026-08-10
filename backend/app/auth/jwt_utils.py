import datetime as dt
import jwt
from ..config import get_settings


def create_token(user_id: int, username: str, role: str) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": dt.datetime.now(dt.UTC) + dt.timedelta(hours=s.jwt_hours),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
