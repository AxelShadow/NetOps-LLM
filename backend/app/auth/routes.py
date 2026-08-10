from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User, Role
from ..config import get_settings
from .ldap_auth import ad_authenticate, split_upn
from .jwt_utils import create_token
from .deps import get_current_user, require_admin

router = APIRouter(prefix="/api")


class LoginIn(BaseModel):
    username: str
    password: str


class UserCreate(BaseModel):
    username: str                    # user@corp.local или просто user
    display_name: str = ""
    role: Role = Role.viewer


class UserPatch(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    display_name: str | None = None


def _normalize(username: str) -> str:
    parsed = split_upn(username)
    if parsed:
        name, domain = parsed
        if domain != get_settings().ad_domain.lower():
            raise HTTPException(
                400, f"Ожидается домен {get_settings().ad_domain}")
        return name
    return username.strip().lower()


# ---------- вход ----------

@router.post("/auth/login")
def login(data: LoginIn, db: Session = Depends(get_db)):
    if not ad_authenticate(data.username, data.password):
        raise HTTPException(401, "Неверный логин или пароль")
    name, _ = split_upn(data.username)
    user = db.query(User).filter(User.username == name).first()
    if not user:
        raise HTTPException(
            403, "Доступ не предоставлен. Обратитесь к администратору.")
    if not user.is_active:
        raise HTTPException(403, "Учётная запись отключена.")
    return {
        "token": create_token(user.id, user.username, user.role.value),
        "user": {"username": user.username,
                 "display_name": user.display_name,
                 "role": user.role.value},
    }


@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"username": user.username, "display_name": user.display_name,
            "role": user.role.value}


# ---------- управление доступом (только админ) ----------

@router.get("/users")
def list_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    users = db.query(User).order_by(User.username).all()
    return [{"id": u.id, "username": u.username, "display_name": u.display_name,
             "role": u.role.value, "is_active": u.is_active,
             "granted_by": u.granted_by, "granted_at": u.granted_at.isoformat()}
            for u in users]


@router.post("/users")
def add_user(data: UserCreate, db: Session = Depends(get_db),
             admin: User = Depends(require_admin)):
    name = _normalize(data.username)
    if db.query(User).filter(User.username == name).first():
        raise HTTPException(409, "Пользователь уже добавлен")
    u = User(username=name, display_name=data.display_name or name,
             role=data.role, granted_by=admin.username)
    db.add(u)
    db.commit()
    return {"id": u.id, "username": u.username}


@router.patch("/users/{uid}")
def patch_user(uid: int, data: UserPatch, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if u.id == admin.id and (data.is_active is False or
                             (data.role is not None and data.role != Role.admin)):
        raise HTTPException(400, "Нельзя лишить прав самого себя")
    if data.role is not None:
        u.role = data.role
    if data.is_active is not None:
        u.is_active = data.is_active
    if data.display_name is not None:
        u.display_name = data.display_name
    db.commit()
    return {"ok": True}


@router.delete("/users/{uid}")
def delete_user(uid: int, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    if uid == admin.id:
        raise HTTPException(400, "Нельзя удалить самого себя")
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    db.delete(u)
    db.commit()
    return {"ok": True}
