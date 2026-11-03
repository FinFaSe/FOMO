"use client";

import { useCallback, useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT } from "./session";
import type { AuditItem, AuthStatus, Role, TokenItem, UserItem } from "./types";

async function call<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, { method, cache: "no-store", headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json();
  if (r.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!r.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Datos inválidos");
  return data;
}
const when = (iso: string | null) => iso ? new Date(iso).toLocaleString("es-PE", { timeZone: "America/Lima", dateStyle: "short", timeStyle: "short" }) : "—";

export default function AccountPanel({ auth }: { auth: AuthStatus }) {
  const isAdmin = auth.user?.role === "admin";
  const [notice, setNotice] = useState<{ text: string; error?: boolean } | null>(null);
  const [tokens, setTokens] = useState<TokenItem[]>([]);
  const [fresh, setFresh] = useState<TokenItem | null>(null);
  const [users, setUsers] = useState<UserItem[]>([]);
  const [audit, setAudit] = useState<AuditItem[]>([]);
  const [pw, setPw] = useState({ current: "", next: "" });
  const [tokenName, setTokenName] = useState("");
  const [draft, setDraft] = useState<{ username: string; password: string; role: Role }>({ username: "", password: "", role: "viewer" });

  const fail = (e: unknown) => setNotice({ text: e instanceof Error ? e.message : "Error", error: true });
  const load = useCallback(() => {
    call<{ items: TokenItem[] }>("/auth/tokens").then(r => setTokens(r.items)).catch(fail);
    if (isAdmin) {
      call<{ items: UserItem[] }>("/users").then(r => setUsers(r.items)).catch(fail);
      call<{ items: AuditItem[] }>("/audit?limit=50").then(r => setAudit(r.items)).catch(fail);
    }
  }, [isAdmin]);
  useEffect(() => { load(); }, [load]);

  async function changePassword(e: React.FormEvent) {
    e.preventDefault();
    try {
      const r = await call<{ sessions_ended: number }>("/auth/password", "POST", { current_password: pw.current, new_password: pw.next });
      setPw({ current: "", next: "" });
      setNotice({ text: `Contraseña cambiada. ${r.sessions_ended} sesión(es) en otros navegadores cerradas.` });
    } catch (err) { fail(err); }
  }
  async function createToken(e: React.FormEvent) {
    e.preventDefault();
    try { setFresh(await call<TokenItem>("/auth/tokens", "POST", { name: tokenName })); setTokenName(""); load(); }
    catch (err) { fail(err); }
  }
  async function revoke(id: number) {
    try { await call(`/auth/tokens/${id}/revoke`, "POST", {}); load(); } catch (err) { fail(err); }
  }
  async function createUser(e: React.FormEvent) {
    e.preventDefault();
    try { await call("/users", "POST", draft); setDraft({ username: "", password: "", role: "viewer" }); setNotice({ text: "Usuario creado" }); load(); }
    catch (err) { fail(err); }
  }
  async function updateUser(u: UserItem, change: Partial<{ role: Role; active: boolean; password: string }>) {
    try { await call(`/users/${u.id}`, "PATCH", change); setNotice({ text: `${u.username} actualizado; sus sesiones abiertas se cerraron` }); load(); }
    catch (err) { fail(err); }
  }
  function resetPassword(u: UserItem) {
    const password = window.prompt(`Nueva contraseña para ${u.username} (mínimo 12 caracteres)`);
    if (password) updateUser(u, { password });
  }

  return <>
    {notice && <div className={notice.error ? "message error" : "message"} role="status"><span>{notice.text}</span><button onClick={() => setNotice(null)} aria-label="Cerrar">×</button></div>}
    <section className="panel transactions">
      <div className="section-heading"><h2>Tu cuenta</h2><span className="badge">{auth.user?.username} · {auth.roles[auth.user?.role ?? "viewer"]}</span></div>
      <form className="inline-form" onSubmit={changePassword}>
        <label>Contraseña actual<input type="password" autoComplete="current-password" value={pw.current} onChange={e => setPw({ ...pw, current: e.target.value })} required /></label>
        <label>Nueva contraseña (12+ caracteres)<input type="password" autoComplete="new-password" minLength={12} value={pw.next} onChange={e => setPw({ ...pw, next: e.target.value })} required /></label>
        <button className="secondary" disabled={!pw.current || pw.next.length < 12}>Cambiar contraseña</button>
      </form>
    </section>

    <section className="panel transactions">
      <div className="section-heading"><h2>Tokens personales</h2><span className="micro">PARA SCRIPTS · MISMO ROL QUE TU USUARIO</span></div>
      <p className="muted">Úsalos con la cabecera <code>Authorization: Bearer …</code>. El valor se muestra una sola vez; solo se guarda su huella.</p>
      {fresh?.token && <div className="evidence-box"><strong>Copia el token «{fresh.name}» ahora:</strong><br/><code className="address">{fresh.token}</code></div>}
      <form className="inline-form" onSubmit={createToken}>
        <label>Nombre<input value={tokenName} maxLength={80} onChange={e => setTokenName(e.target.value)} placeholder="p. ej. informe diario" required /></label>
        <button className="secondary" disabled={!tokenName}>Crear token</button>
      </form>
      {tokens.length > 0 && <div className="table-wrap"><table><thead><tr><th>Nombre</th><th>Creado</th><th>Último uso</th><th>Estado</th><th></th></tr></thead><tbody>
        {tokens.map(t => <tr key={t.id}><td>{t.name}</td><td>{when(t.created_at)}</td><td>{when(t.last_used_at)}</td>
          <td><span className={t.revoked ? "badge warning" : "badge green"}>{t.revoked ? "revocado" : "activo"}</span></td>
          <td>{!t.revoked && <button className="quiet" onClick={() => revoke(t.id)}>Revocar</button>}</td></tr>)}</tbody></table></div>}
    </section>

    {isAdmin && <section className="panel transactions">
      <div className="section-heading"><h2>Usuarios</h2><span className="micro">LECTOR · ANALISTA · ADMINISTRADOR</span></div>
      <p className="muted">Lector consulta; analista también opera (sincronizar, watchlist, semillas, backtests); administrador además gestiona usuarios y ve la auditoría. Cambiar el rol, la contraseña o desactivar cierra las sesiones abiertas del usuario.</p>
      <form className="inline-form" onSubmit={createUser}>
        <label>Usuario<input value={draft.username} maxLength={64} onChange={e => setDraft({ ...draft, username: e.target.value })} required /></label>
        <label>Contraseña inicial<input type="password" autoComplete="new-password" minLength={12} value={draft.password} onChange={e => setDraft({ ...draft, password: e.target.value })} required /></label>
        <label>Rol<select value={draft.role} onChange={e => setDraft({ ...draft, role: e.target.value as Role })}>
          {(Object.keys(auth.roles) as Role[]).map(r => <option key={r} value={r}>{auth.roles[r]}</option>)}</select></label>
        <button className="primary" disabled={!draft.username || draft.password.length < 12}>Crear usuario</button>
      </form>
      <div className="table-wrap"><table><thead><tr><th>Usuario</th><th>Rol</th><th>Estado</th><th>Último acceso</th><th></th></tr></thead><tbody>
        {users.map(u => <tr key={u.id}><td>{u.username}{u.username === auth.user?.username ? <small> (tú)</small> : null}</td>
          <td><select aria-label={`Rol de ${u.username}`} value={u.role} onChange={e => updateUser(u, { role: e.target.value as Role })}>
            {(Object.keys(auth.roles) as Role[]).map(r => <option key={r} value={r}>{auth.roles[r]}</option>)}</select></td>
          <td><span className={u.active ? (u.locked ? "badge warning" : "badge green") : "badge"}>{u.active ? (u.locked ? "bloqueado" : "activo") : "desactivado"}</span></td>
          <td>{when(u.last_login_at)}</td>
          <td><button className="quiet" onClick={() => updateUser(u, { active: !u.active })}>{u.active ? "Desactivar" : "Activar"}</button>
            <button className="quiet" onClick={() => resetPassword(u)}>Nueva contraseña</button></td></tr>)}</tbody></table></div>
    </section>}

    {isAdmin && <section className="panel transactions">
      <div className="section-heading"><h2>Auditoría</h2><span className="micro">ÚLTIMOS 50 EVENTOS</span></div>
      <div className="table-wrap"><table><thead><tr><th>Hora · Lima</th><th>Usuario</th><th>Acción</th><th>Estado</th><th>IP</th></tr></thead><tbody>
        {audit.map(a => <tr key={a.id}><td>{when(a.at)}</td><td>{a.user ?? "—"}</td><td className="mono">{a.action}</td><td>{a.status ?? "—"}</td><td className="mono">{a.ip ?? "—"}</td></tr>)}</tbody></table></div>
    </section>}
  </>;
}
