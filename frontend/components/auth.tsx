"use client";

import { useCallback, useEffect, useState } from "react";
import Tracker from "./tracker";
import { UNAUTHORIZED_EVENT } from "./session";
import type { AuthStatus } from "./types";

async function call<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method: body === undefined ? "GET" : "POST", cache: "no-store",
    headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "No se pudo completar la operación");
  return data;
}

function Login({ status, onSignedIn }: { status: AuthStatus; onSignedIn: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try { await call("/auth/login", { username, password }); setPassword(""); onSignedIn(); }
    catch (err) { setError(err instanceof Error ? err.message : "No se pudo iniciar sesión"); }
    finally { setBusy(false); }
  }
  return <main className="login-shell">
    <form className="panel login-panel" onSubmit={submit}>
      <div className="brand"><span className="brand-mark">f.</span><div>FOMO<span>SMART MONEY RESEARCH</span></div></div>
      <h1>Inicia sesión</h1>
      {status.setup_required ? <p className="evidence-box">Aún no hay usuarios. Crea el primer administrador en el servidor:<br/>
        <code>python -m tracker.users create TU_USUARIO</code></p> : <p className="muted">Acceso restringido a los usuarios de esta instancia.</p>}
      {error && <div className="message error" role="alert">{error}</div>}
      <label>Usuario<input autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} required maxLength={64} /></label>
      <label>Contraseña<input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required maxLength={256} /></label>
      <button className="primary" disabled={busy || !username || !password}>{busy ? "Entrando…" : "Entrar"}</button>
      <p className="muted">Cinco intentos fallidos bloquean la cuenta durante 15 minutos.</p>
    </form>
  </main>;
}

export default function AuthGate() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState("");
  const refresh = useCallback(() => {
    call<AuthStatus>("/auth/status").then(s => { setStatus(s); setError(""); })
      .catch(e => setError(e instanceof Error ? e.message : "Backend no disponible"));
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    window.addEventListener(UNAUTHORIZED_EVENT, refresh);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, refresh);
  }, [refresh]);
  const signOut = useCallback(async () => {
    try { await call("/auth/logout", {}); } finally { refresh(); }
  }, [refresh]);
  if (!status) return <main className="login-shell"><div className="panel login-panel">{error ? <div className="message error">{error}</div> : <p className="muted">Conectando…</p>}</div></main>;
  if (status.mode === "on" && !status.authenticated) return <Login status={status} onSignedIn={refresh} />;
  return <Tracker auth={status} onSignOut={signOut} />;
}
