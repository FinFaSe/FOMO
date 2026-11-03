"""Sign-in, sign-out, passwords, personal tokens, user management and the audit log (see tracker.auth)."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select

from . import auth
from .db import ApiToken, AuditEvent, User
from .schemas import LoginRequest, PasswordChange, TokenCreate, UserCreate, UserUpdate


def client_ip(request: Request) -> str | None:
    return getattr(request.state, "client_ip", None) or (request.client.host if request.client else None)


def build_router(session) -> APIRouter:
    router = APIRouter()

    def principal(request: Request) -> auth.Principal:
        user = getattr(request.state, "principal", None)
        if user is None:  # only reachable with TRACKER_AUTH=off
            raise HTTPException(400, "El login está desactivado (TRACKER_AUTH=off)")
        return user

    @router.get("/api/auth/status")
    def status(request: Request, s=Depends(session)):
        on = auth.enabled()
        user = None
        if on:
            user = auth.resolve(s, cookie=request.cookies.get(auth.SESSION_COOKIE),
                                authorization=request.headers.get("authorization"))
        return {"mode": "on" if on else "off", "authenticated": user is not None,
                "user": {"username": user.username, "role": user.role, "via": user.via} if user else None,
                "setup_required": on and s.scalar(select(User.id).limit(1)) is None,
                "roles": auth.ROLE_NAMES}

    @router.post("/api/auth/login")
    def login(body: LoginRequest, request: Request, response: Response, s=Depends(session)):
        if not auth.enabled():
            raise HTTPException(400, "El login está desactivado (TRACKER_AUTH=off)")
        user = auth.authenticate(s, body.username, body.password)
        if user is None:
            auth.audit(s, body.username[:64], "login_failed", status=401, ip=client_ip(request))
            raise HTTPException(401, "Usuario o contraseña incorrectos, o cuenta bloqueada temporalmente tras "
                                     "varios intentos")
        token = auth.create_session(s, user)
        response.set_cookie(auth.SESSION_COOKIE, token, max_age=int(auth.SESSION_MAX.total_seconds()), path="/",
                            httponly=True, secure=auth.cookie_secure(), samesite="strict")
        auth.audit(s, user.username, "login", status=200, ip=client_ip(request))
        return {"user": {"username": user.username, "role": user.role}}

    @router.post("/api/auth/logout")
    def logout(request: Request, response: Response, s=Depends(session)):
        user = principal(request)
        if user.token:
            auth.end_session(s, user.token)
        response.delete_cookie(auth.SESSION_COOKIE, path="/", httponly=True, secure=auth.cookie_secure(),
                               samesite="strict")
        auth.audit(s, user.username, "logout", status=200, ip=client_ip(request))
        return {"ok": True}

    @router.post("/api/auth/password")
    def change_password(body: PasswordChange, request: Request, s=Depends(session)):
        user = principal(request)
        row = s.get(User, user.id)
        if not auth.verify_password(body.current_password, row.password_hash):
            auth.audit(s, user.username, "password_change_failed", status=401, ip=client_ip(request))
            raise HTTPException(401, "La contraseña actual no es correcta")
        if problem := auth.password_problem(body.new_password, row.username):
            raise HTTPException(422, problem)
        row.password_hash = auth.hash_password(body.new_password)
        s.commit()
        ended = auth.end_sessions(s, row.id, keep=user.token)  # other browsers must sign in again
        auth.audit(s, user.username, "password_changed", status=200, ip=client_ip(request),
                   details={"sessions_ended": ended})
        return {"ok": True, "sessions_ended": ended}

    def token_json(t: ApiToken) -> dict:
        return {"id": t.id, "name": t.name, "created_at": t.created_at, "last_used_at": t.last_used_at,
                "revoked": t.revoked}

    @router.get("/api/auth/tokens")
    def tokens(request: Request, s=Depends(session)):
        user = principal(request)
        rows = s.scalars(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.id.desc()))
        return {"items": [token_json(t) for t in rows]}

    @router.post("/api/auth/tokens", status_code=201)
    def create_token(body: TokenCreate, request: Request, s=Depends(session)):
        user = principal(request)
        row, token = auth.create_api_token(s, s.get(User, user.id), body.name)
        auth.audit(s, user.username, "token_created", status=201, ip=client_ip(request), details={"id": row.id})
        # The value is shown once; only its hash is stored.
        return {"token": token, **token_json(row)}

    @router.post("/api/auth/tokens/{token_id}/revoke")
    def revoke_token(token_id: int, request: Request, s=Depends(session)):
        user = principal(request)
        row = s.get(ApiToken, token_id)
        if row is None or (row.user_id != user.id and not user.can("admin")):
            raise HTTPException(404, "Token no encontrado")
        row.revoked = True
        s.commit()
        auth.audit(s, user.username, "token_revoked", status=200, ip=client_ip(request), details={"id": token_id})
        return token_json(row)

    @router.get("/api/users")
    def users(s=Depends(session)):
        return {"items": [auth.user_json(u) for u in s.scalars(select(User).order_by(User.username))]}

    @router.post("/api/users", status_code=201)
    def create_user(body: UserCreate, request: Request, s=Depends(session)):
        if problem := auth.username_problem(body.username) or auth.password_problem(body.password, body.username):
            raise HTTPException(422, problem)
        if s.scalar(select(User.id).where(User.username == body.username)):
            raise HTTPException(409, "Ese usuario ya existe")
        row = User(username=body.username, password_hash=auth.hash_password(body.password), role=body.role)
        s.add(row)
        s.commit()
        auth.audit(s, principal(request).username, "user_created", status=201, ip=client_ip(request),
                   details={"user": body.username, "role": body.role})
        return auth.user_json(row)

    @router.patch("/api/users/{user_id}")
    def update_user(user_id: int, body: UserUpdate, request: Request, s=Depends(session)):
        row = s.get(User, user_id)
        if row is None:
            raise HTTPException(404, "Usuario no encontrado")
        demoted = (body.role is not None and body.role != "admin") or body.active is False
        if row.role == "admin" and row.active and demoted and auth.active_admins(s) <= 1:
            raise HTTPException(409, "Debe quedar al menos un administrador activo")
        changes = {}
        if body.password is not None:
            if problem := auth.password_problem(body.password, row.username):
                raise HTTPException(422, problem)
            row.password_hash, row.failed_logins, row.locked_until = auth.hash_password(body.password), 0, None
            changes["password"] = "reset"
        if body.role is not None:
            row.role, changes["role"] = body.role, body.role
        if body.active is not None:
            row.active, changes["active"] = body.active, body.active
        s.commit()
        if body.password is not None or body.active is False or body.role is not None:
            changes["sessions_ended"] = auth.end_sessions(s, row.id)  # the new rights apply from the next sign-in
        if body.active is False:
            auth.revoke_tokens(s, row.id)
        auth.audit(s, principal(request).username, "user_updated", status=200, ip=client_ip(request),
                   details={"user": row.username, **changes})
        return auth.user_json(row)

    @router.get("/api/audit")
    def audit_log(limit: int = Query(100, ge=1, le=500), before_id: int | None = None, s=Depends(session)):
        query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
        if before_id is not None:
            query = query.where(AuditEvent.id < before_id)
        return {"items": [{"id": e.id, "at": e.at, "user": e.username, "action": e.action, "status": e.status,
                           "ip": e.ip, "details": e.details} for e in s.scalars(query)]}

    return router
