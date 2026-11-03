"""User administration from the command line (same DATABASE_URL as the API). No default credentials exist: the first
admin is created here, then more users can be managed from the dashboard.

    python -m tracker.users create USERNAME [--role viewer|analyst|admin] [--password-stdin]
    python -m tracker.users list
    python -m tracker.users set-role USERNAME ROLE
    python -m tracker.users disable USERNAME | enable USERNAME
    python -m tracker.users reset-password USERNAME [--password-stdin]

Passwords are read without echo (or from stdin with --password-stdin, for automation) and never printed.
"""

import argparse
import getpass
import sys

from sqlalchemy import select

from . import auth
from .db import User


def _password(args, username: str) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Contraseña: ")
        if getpass.getpass("Repite la contraseña: ") != password:
            raise SystemExit("Las contraseñas no coinciden")
    if problem := auth.password_problem(password, username):
        raise SystemExit(problem)
    return password


def _user(session, username: str) -> User:
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        raise SystemExit(f"No existe el usuario {username}")
    return user


def run(session, args) -> str:
    if args.command == "create":
        if problem := auth.username_problem(args.username):
            raise SystemExit(problem)
        if session.scalar(select(User.id).where(User.username == args.username)):
            raise SystemExit(f"El usuario {args.username} ya existe")
        session.add(User(username=args.username, password_hash=auth.hash_password(_password(args, args.username)),
                         role=args.role))
        session.commit()
        auth.audit(session, None, "user_created", details={"user": args.username, "role": args.role, "via": "cli"})
        return f"Usuario {args.username} creado con rol {args.role}"
    if args.command == "list":
        rows = session.scalars(select(User).order_by(User.username))
        return "\n".join(f"{u.username}\t{u.role}\t{'activo' if u.active else 'desactivado'}\t"
                         f"último acceso: {u.last_login_at or '—'}" for u in rows) or "Sin usuarios"
    user = _user(session, args.username)
    if args.command in ("set-role", "disable") and user.role == "admin" and user.active and (
            args.command == "disable" or args.role != "admin") and auth.active_admins(session) <= 1:
        raise SystemExit("Debe quedar al menos un administrador activo")
    if args.command == "set-role":
        user.role = args.role
    elif args.command in ("disable", "enable"):
        user.active = args.command == "enable"
    elif args.command == "reset-password":
        user.password_hash = auth.hash_password(_password(args, user.username))
        user.failed_logins, user.locked_until = 0, None
    session.commit()
    ended = auth.end_sessions(session, user.id)
    if args.command == "disable":
        auth.revoke_tokens(session, user.id)
    auth.audit(session, None, f"user_{args.command.replace('-', '_')}", details={"user": user.username, "via": "cli"})
    return f"{user.username}: hecho ({ended} sesiones cerradas)"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tracker.users", description="Usuarios del tracker")
    sub = p.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("username")
    create.add_argument("--role", choices=list(auth.ROLES), default="admin")
    create.add_argument("--password-stdin", action="store_true")
    sub.add_parser("list")
    role = sub.add_parser("set-role")
    role.add_argument("username")
    role.add_argument("role", choices=list(auth.ROLES))
    for name in ("disable", "enable"):
        sub.add_parser(name).add_argument("username")
    reset = sub.add_parser("reset-password")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")
    return p


def main(argv=None) -> int:
    from .db import database

    args = parser().parse_args(argv)
    _, sessions = database()
    with sessions() as session:
        print(run(session, args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
