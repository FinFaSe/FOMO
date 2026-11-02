"use client";

import { useCallback, useEffect, useState } from "react";
import type { Analysis, Config, Filters, Observation, Wallet } from "./types";

const initial: Filters = { max_entry_market_cap_usd: "250000", min_realized_roi_pct: "500",
  min_win_rate: 0.6, min_evaluable_positions: 30, analysis_days: 90, conservative_win_rate: true };
const short = (s: string) => `${s.slice(0, 5)}…${s.slice(-5)}`;
const money = (s: string | number | null | undefined) => s == null ? "Sin datos" :
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(s));
const pct = (n: number | null | undefined) => n == null ? "Sin datos" : `${(n * 100).toFixed(1)}%`;
const date = (s: string) => new Intl.DateTimeFormat("es-PE", { dateStyle: "medium", timeStyle: "short", timeZone: "America/Lima" }).format(new Date(s));

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, { ...init, headers: { "Content-Type": "application/json" }, cache: "no-store" });
  const body = await r.json();
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Datos inválidos. Revisa el formato y los campos obligatorios.");
  return body;
}
const post = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });
function query(filters: Filters) { return new URLSearchParams(Object.entries(filters).map(([k, v]) => [k, String(v)])).toString(); }

const reasons: Record<string, string> = {
  insufficient_evaluable_positions: "Menos posiciones evaluables de las exigidas",
  observed_win_rate_below_threshold: "Win rate observado inferior al umbral",
  wilson_lower_bound_below_threshold: "El límite inferior de confianza no alcanza el win rate mínimo",
  no_early_realized_big_winner: "Sin gran ganador realizado con entradas bajo el market cap máximo",
  unverified_trades_usd_or_historical_market_cap: "Faltan swaps, costos USD o capitalización histórica verificados",
  incomplete_history: "Historial normalizado incompleto",
  unresolved_inventory_or_transfer_risk: "Transferencias o inventario sin resolver",
  no_evaluable_closed_positions: "Aún no hay posiciones cerradas evaluables",
  incomplete_position_coverage: "Hay cierres excluidos por costos o inventario desconocidos; podrían ocultar pérdidas",
};
const components: Record<string, string> = {
  temporal_consistency: "Consistencia temporal / 25", big_winner_frequency: "Ganadores ≥ umbral / 20",
  roi_1000_frequency: "Ganadores ≥ 1.000% / 15", net_risk_adjusted_return: "Retorno y riesgo / 15",
  early_entry: "Entradas tempranas / 10", replicability: "Replicabilidad / 10", data_quality: "Calidad de datos / 5",
};

function PnlChart({ points }: { points: Analysis["realized_pnl_curve"] }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!points.length) return <div className="chart-empty">La curva aparecerá cuando haya posiciones cerradas con costo conocido.</div>;
  const values = [0, ...points.map(p => Number(p.cumulative_realized_pnl_usd))];
  const low = Math.min(...values), high = Math.max(...values), range = high - low || 1;
  const x = (i: number) => 20 + i / Math.max(1, values.length - 1) * 740;
  const y = (v: number) => 155 - (v - low) / range * 130;
  const current = hover == null ? null : points[hover];
  return <div className="chart" onMouseLeave={() => setHover(null)}>
    <div className="chart-caption">{current ? `${date(current.timestamp)} · ${money(current.cumulative_realized_pnl_usd)}` : "Selecciona un punto para inspeccionar el PnL acumulado"}</div>
    <svg viewBox="0 0 780 185" role="img" aria-label="Curva de PnL realizado acumulado en USD">
      <line x1="20" x2="760" y1={y(0)} y2={y(0)} stroke="#34413c" strokeDasharray="4 5" />
      <polyline fill="none" stroke="#b8ed6b" strokeWidth="2.5" points={values.map((v, i) => `${x(i)},${y(v)}`).join(" ")} />
      {points.map((p, i) => <circle key={i} cx={x(i + 1)} cy={y(Number(p.cumulative_realized_pnl_usd))} r={hover === i ? 6 : 3}
        fill="#b8ed6b" tabIndex={0} onFocus={() => setHover(i)} onMouseEnter={() => setHover(i)}>
        <title>{date(p.timestamp)}: {money(p.cumulative_realized_pnl_usd)}</title>
      </circle>)}
      <text x="20" y="180" fill="#8c9b96" fontSize="11">Inicio de ventana</text>
      <text x="760" y="180" textAnchor="end" fill="#8c9b96" fontSize="11">Último cierre</text>
    </svg>
  </div>;
}

export default function Tracker() {
  const [tab, setTab] = useState("overview");
  const [config, setConfig] = useState<Config | null>(null);
  const [wallets, setWallets] = useState<Wallet[]>([]);
  const [walletTotal, setWalletTotal] = useState(0);
  const [selected, setSelected] = useState("");
  const [filters, setFilters] = useState<Filters>(initial);
  const [draft, setDraft] = useState<Filters>(initial);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [observations, setObservations] = useState<Observation[]>([]);
  const [observationTotal, setObservationTotal] = useState(0);
  const [observationOffset, setObservationOffset] = useState(0);
  const [verifyOffset, setVerifyOffset] = useState(0);
  const [rankings, setRankings] = useState<{ address: string; label: string; metrics: Analysis["metrics"] }[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [handle, setHandle] = useState("");
  const [seed, setSeed] = useState("");
  const [expanded, setExpanded] = useState("");
  const wallet = wallets.find(w => w.address === selected);

  const reloadWallets = useCallback(async () => {
    const list = await api<{items: Wallet[]; total: number}>("/wallets?limit=200");
    setWallets(list.items); setWalletTotal(list.total);
  }, []);
  useEffect(() => {
    setVerifyOffset(0);
  }, [selected]);

  useEffect(() => {
    Promise.all([api<Config>("/config").then(c => { setConfig(c); setDraft(c.default_filters); setFilters(c.default_filters); }),
      reloadWallets()]).catch(e => setError(e.message));
  }, [reloadWallets]);

  useEffect(() => {
    let active = true;
    setAnalysis(null); setObservations([]); setObservationTotal(0);
    if (selected) {
      Promise.all([api<Analysis>(`/wallets/${selected}/analysis?${query(filters)}`),
        api<{ items: Observation[]; total: number }>(`/wallets/${selected}/observations?limit=20&offset=${observationOffset}`)])
        .then(([a, o]) => { if (active) { setAnalysis(a); setObservations(o.items); setObservationTotal(o.total); } })
        .catch(e => { if (active) setError(e.message); });
    }
    return () => { active = false; };
  }, [selected, filters, wallet?.sync, observationOffset]);

  useEffect(() => {
    let active = true;
    api<{ items: typeof rankings }>(`/rankings?${query(filters)}`).then(r => { if (active) setRankings(r.items); })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [filters, wallets]);

  async function action(name: string, task: () => Promise<void>) {
    setBusy(name); setError(""); setNotice("");
    try { await task(); } catch (e) { setError(e instanceof Error ? e.message : "Error inesperado"); }
    finally { setBusy(""); }
  }

  async function addWallet(e: React.FormEvent) {
    e.preventDefault();
    await action("add", async () => {
      const w = await api<Wallet>("/wallets", post({ address: address.trim(), label: label.trim() }));
      await reloadWallets(); setSelected(w.address); setObservationOffset(0); setAddress(""); setLabel("");
      setNotice("Wallet añadida. Sincroniza para consultar sus transacciones públicas.");
    });
  }
  async function sync() {
    await action("sync", async () => {
      const r = await api<{state: string; transactions: number; issues: string[]}>(`/wallets/${selected}/sync`,
        post({ days: filters.analysis_days, max_pages: 2, page_size: 10 }));
      await reloadWallets();
      setNotice(`${r.transactions} transacciones procesadas. ${r.state === "partial" ? "Historial parcial: " + r.issues.join(", ") : "Ventana de firmas escaneada; falta normalización financiera completa."}`);
    });
  }
  async function importFile(file: File) {
    await action("import", async () => {
      if (file.size > 3_000_000) throw new Error("El archivo supera 3 MB.");
      const payload = JSON.parse(await file.text());
      const r = await api<{ inserted: number; duplicates: number }>(`/wallets/${selected}/ledger`, post(payload));
      setVerifyOffset(0);
      const a = await api<Analysis>(`/wallets/${selected}/analysis?${query(filters)}`); setAnalysis(a);
      setNotice(`${r.inserted} eventos importados, ${r.duplicates} duplicados. Datos declarados, aún sin verificar.`);
    });
  }
  const tabs = [{id: "overview", name: "Explorador", icon: "◈"}, {id: "rankings", name: "Ranking validado", icon: "↗"},
    {id: "watchlist", name: "Watchlist", icon: "☆"}, {id: "sources", name: "Fuentes y método", icon: "◎"}];

  return <div className="shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="Inicio Smart Money"><span className="brand-mark">f.</span><div>FOMO<span>SMART MONEY RESEARCH</span></div></a>
      <div className="workspace-label">ESPACIO DE INVESTIGACIÓN</div>
      <nav aria-label="Navegación principal">{tabs.map(t => <button key={t.id} className={tab === t.id ? "nav-item active" : "nav-item"}
        onClick={() => setTab(t.id)}><span>{t.icon}</span>{t.name}{t.id === "watchlist" && <small>{wallets.filter(w => w.favorite).length}</small>}</button>)}</nav>
      <div className="sidebar-note"><span className="status-dot" /> Solana · mainnet<div>Solo datos reales<br/>Sin ejecución de operaciones</div></div>
      <div className="independent">Herramienta de investigación independiente. Sin afiliación con fomo.family.</div>
    </aside>
    <main>
      <header className="topbar"><span>Investigación <span className="slash">/</span> {tabs.find(t => t.id === tab)?.name}</span>
        <div className="network-pill"><span className={config ? "status-dot" : "status-dot muted-dot"} />{config ? "Backend conectado" : "Conectando…"}</div></header>
      <div className="content">
        <div className="page-heading"><div><div className="eyebrow">EVIDENCIA ANTES QUE RENDIMIENTO</div>
          <h1>Encuentra habilidad.<br className="mobile-break"/><span> Exige pruebas.</span></h1>
          <p>Entradas tempranas, beneficios realizados y consistencia que puedas verificar.</p></div>
          <span className="version-tag">SOLANA <span>FASE 01</span></span></div>
        {error && <div className="message error" role="alert">{error}<button aria-label="Cerrar error" onClick={() => setError("")}>×</button></div>}
        {notice && <div className="message" role="status">{notice}<button aria-label="Cerrar mensaje" onClick={() => setNotice("")}>×</button></div>}
        <form className="filters panel" onSubmit={e => { e.preventDefault(); setFilters({...draft}); setNotice("Filtros aplicados al análisis y al ranking."); }}>
          <div className="filter-heading"><span>⌕ &nbsp; Criterios de selección</span><span className="micro">EDITABLES · USD</span></div>
          <div className="filter-grid">
            <label>Market cap máximo de entrada<input aria-label="Market cap máximo de entrada" type="number" min="0" required value={draft.max_entry_market_cap_usd}
              onChange={e => setDraft({...draft, max_entry_market_cap_usd: e.target.value})}/></label>
            <label>ROI realizado mínimo (%)<input type="number" min="0" required value={draft.min_realized_roi_pct}
              onChange={e => setDraft({...draft, min_realized_roi_pct: e.target.value})}/></label>
            <label>Win rate mínimo (%)<input type="number" min="0" max="100" required step="1" value={Math.round(draft.min_win_rate * 100)}
              onChange={e => setDraft({...draft, min_win_rate: Number(e.target.value) / 100})}/></label>
            <label>Posiciones evaluables mínimas<input type="number" min="1" max="100000" required value={draft.min_evaluable_positions}
              onChange={e => setDraft({...draft, min_evaluable_positions: Number(e.target.value)})}/></label>
            <label>Ventana de análisis<select value={draft.analysis_days} onChange={e => setDraft({...draft, analysis_days: Number(e.target.value)})}>
              {[7, 30, 90, 180, 365].map(n => <option key={n} value={n}>{n} días</option>)}</select></label>
            <button className="primary" type="submit" disabled={!!busy}>Aplicar filtros <span>↗</span></button>
          </div>
          <label className="checkbox"><input type="checkbox" checked={draft.conservative_win_rate}
            onChange={e => setDraft({...draft, conservative_win_rate: e.target.checked})}/> Exigir el umbral también al límite inferior de Wilson al 95%, agrupado por token</label>
        </form>

        <section className="stats" aria-label="Resumen del espacio">
          {[{label:"Wallets en estudio",value:String(walletTotal),note:"Añadidas o descubiertas"},
            {label:"Traders validados",value:String(rankings.length),note:"En la página actual del ranking"},
            {label:"Muestra mínima",value:String(filters.min_evaluable_positions),note:"Posiciones cerradas evaluables"},
            {label:"Gran ganador",value:`+${filters.min_realized_roi_pct}%`,note:`${1 + Number(filters.min_realized_roi_pct) / 100}× del capital · neto de costos`}].map(s =>
              <article className="stat panel" key={s.label}><span>{s.label}</span><strong>{s.value}</strong><small>{s.note}</small></article>)}
        </section>

        {(tab === "overview" || tab === "watchlist") && <>
          <div className="research-grid">
            <section className="panel wallet-panel"><div className="section-heading"><h2>{tab === "watchlist" ? "Watchlist" : "Wallets en estudio"}</h2><span className="micro">{walletTotal} REGISTROS</span></div>
              <form onSubmit={addWallet} className="wallet-form"><label>Dirección Solana<input placeholder="Dirección pública de la wallet" required value={address} onChange={e => setAddress(e.target.value)}/></label>
                <label>Etiqueta opcional<input placeholder="Nombre para tu investigación" maxLength={80} value={label} onChange={e => setLabel(e.target.value)}/></label>
                <button className="secondary" disabled={!!busy}>{busy === "add" ? "Añadiendo…" : "+ Añadir wallet"}</button></form>
              <div className="wallet-list">{wallets.filter(w => tab !== "watchlist" || w.favorite).map(w => <div className={`wallet-row ${selected === w.address ? "selected" : ""}`} key={w.address}>
                <button className="wallet-select" disabled={!!busy} onClick={() => { setSelected(w.address); setObservationOffset(0); setExpanded(""); }}><span className="avatar">{(w.label || w.address).slice(0,2).toUpperCase()}</span>
                  <span><strong>{w.label || short(w.address)}</strong><code>{short(w.address)}</code></span></button>
                <button className={w.favorite ? "star starred" : "star"} disabled={!!busy} aria-label={`${w.favorite ? "Quitar de" : "Añadir a"} watchlist ${w.address}`}
                  onClick={() => action("favorite", async () => { await api(`/wallets/${w.address}`, {method:"PATCH", body:JSON.stringify({favorite:!w.favorite})}); await reloadWallets(); })}>{w.favorite ? "★" : "☆"}</button>
              </div>)}</div>
              {!wallets.filter(w => tab !== "watchlist" || w.favorite).length && <div className="small-empty">{tab === "watchlist" ? "Marca una wallet con ☆ para seguirla aquí." : "Todavía no hay wallets. Añade una dirección pública para empezar."}</div>}
              {walletTotal > 200 && <p className="muted">Se muestran 200 wallets. El resto está disponible mediante la API paginada.</p>}
            </section>
            <section className="panel profile-panel">
              {!wallet ? <div className="empty"><div className="empty-icon">◎</div><h2>La próxima wallet merece una prueba.</h2><p>Selecciona una wallet para inspeccionar transacciones, reconstruir posiciones y evaluar la calidad de la evidencia.</p><span className="subtle-pill">No cargamos traders de demostración</span></div> : <>
                <div className="section-heading"><div><div className="eyebrow">PERFIL DE INVESTIGACIÓN</div><h2>{wallet.label || short(selected)}</h2></div><span className="badge warning">{analysis?.eligible ? "Validado" : "Evidencia insuficiente"}</span></div>
                <a className="address" href={`https://solscan.io/account/${selected}`} target="_blank" rel="noreferrer">{selected} ↗</a>
                {wallet.identity && <p className="muted">@{wallet.identity.handle} · Asociación declarada por {wallet.identity.source}; sin verificación independiente.</p>}
                <div className="toolbar"><button className="primary" disabled={!!busy} onClick={sync}>{busy === "sync" ? "Consultando Solana…" : "Sincronizar on-chain"}</button>
                  <label className={`secondary file-button ${busy ? "disabled" : ""}`}>Importar ledger JSON<input aria-label="Importar ledger JSON" type="file" accept="application/json,.json" disabled={!!busy}
                    onChange={e => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }}/></label>
                  <button className="quiet" disabled={!!busy} onClick={() => action("verify", async () => {
                    const r = await api<{matched_movements:number; errors:{error:string}[]; next_offset: number | null; remaining_signatures:number}>(`/wallets/${selected}/verify?offset=${verifyOffset}`, post({}));
                    setVerifyOffset(r.next_offset || 0);
                    setAnalysis(await api<Analysis>(`/wallets/${selected}/analysis?${query(filters)}`));
                    setNotice(`${r.matched_movements} movimientos coinciden con la cadena. No verifica USD ni market cap. ${r.remaining_signatures ? `${r.remaining_signatures} firmas pendientes; vuelve a pulsar para continuar. ` : ""}${r.errors.map(e => e.error).join("; ")}`);
                  })}>{verifyOffset ? "Continuar verificación ↗" : "Verificar ledger ↗"}</button></div>
                {wallet.sync && <p className="sync-note">Último escaneo: {wallet.sync.state} · {wallet.sync.transactions} transacciones · {wallet.sync.signature_window_complete ? "ventana de firmas escaneada" : "cobertura parcial"}. Normalización financiera incompleta.</p>}
                {analysis ? <>
                  <div className="profile-metrics">{[
                    ["PnL realizado declarado",money(analysis.metrics.net_realized_pnl_usd)],
                    ["Win rate observado",pct(analysis.metrics.win_rate)],
                    ["Wilson 95% · por token",`${pct(analysis.metrics.win_rate_ci95[0])} – ${pct(analysis.metrics.win_rate_ci95[1])}`],
                    ["Muestra / tokens efectivos",`${analysis.metrics.evaluable_positions} / ${analysis.metrics.effective_sample_tokens}`],
                    ["Grandes ganadores tempranos",String(analysis.metrics.early_big_winners)],
                    ["Score verificado",analysis.score.verified_smart_money == null ? "No disponible" : `${analysis.score.verified_smart_money} / 100`],
                  ].map(([k,v]) => <div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
                  <div className="evidence-box"><strong>Lo que falta para clasificar esta wallet</strong><ul>{analysis.exclusion_reasons.map(r => <li key={r}>{reasons[r] || r}</li>)}</ul>
                    <span>ROI +500% = 6×. Las posiciones abiertas y las transferencias sin costo conocido no cuentan como aciertos.</span></div>
                  <h3>PnL realizado acumulado <span className="micro">DECLARADO · NO ES CAPITAL TOTAL</span></h3><PnlChart points={analysis.realized_pnl_curve}/>
                  <details className="method-details"><summary>Desglose del score exploratorio</summary><p>No es un score validado. Riesgo de copia desconocido; replicabilidad sin medir.</p>
                    {Object.entries(analysis.score.components).map(([k,v]) => <div className="score-row" key={k}><span>{components[k] || k}</span><strong>{v.toFixed(2)}</strong></div>)}
                    <p>Penalización por concentración: {pct(analysis.score.concentration_penalty_fraction)}. Total exploratorio: {analysis.score.historical_skill_provisional ?? "Sin datos"}.</p></details>
                  <div className="snapshot"><span>Snapshot reproducible · {date(analysis.as_of)} (Lima)</span><a href={`/api/snapshots/${analysis.snapshot_id}`} target="_blank" rel="noreferrer">{short(analysis.snapshot_id)} ↗</a></div>
                </> : <p className="muted" role="status">Cargando análisis…</p>}
              </>}
            </section>
          </div>
          {wallet && <section className="panel transactions"><div className="section-heading"><h2>Transacciones públicas</h2><span className="badge">{observationTotal} OBSERVACIONES</span></div>
            <p className="muted">Movimientos finalizados conservados con su fuente. Un cambio de balance no demuestra por sí solo una compra, una venta ni un beneficio.</p>
            {!observations.length ? <div className="small-empty">Sin transacciones almacenadas. Pulsa «Sincronizar on-chain».</div> : <div className="table-wrap"><table><thead><tr><th>Transacción</th><th>Fecha · Lima</th><th>Estado</th><th>Variación SOL</th><th>Tokens con movimiento</th></tr></thead><tbody>{observations.map(o => <tr key={o.signature}>
              <td><a href={o.transaction_url} target="_blank" rel="noreferrer"><code>{short(o.signature)} ↗</code></a></td>
              <td>{o.block_time == null ? "Desconocida" : date(new Date(o.block_time * 1000).toISOString())}</td><td><span className="badge">{o.status}</span></td>
              <td className="mono">{o.native_sol_delta ?? "Desconocida"}</td><td>{Object.keys(o.token_deltas).length}<details><summary>Ver deltas</summary>{Object.entries(o.token_deltas).map(([k,v]) => <p className="mono" key={k}>{short(k)}: {v}</p>)}</details></td>
            </tr>)}</tbody></table></div>}
            {observationTotal > 20 && <div className="pagination"><button className="quiet" disabled={!observationOffset} onClick={() => setObservationOffset(Math.max(0,observationOffset-20))}>← Anteriores</button>
              <span>{observationOffset+1}–{Math.min(observationOffset+20,observationTotal)} de {observationTotal}</span><button className="quiet" disabled={observationOffset+20 >= observationTotal} onClick={() => setObservationOffset(observationOffset+20)}>Siguientes →</button></div>}
          </section>}
          {analysis && <section className="panel transactions"><div className="section-heading"><h2>Posiciones reconstruidas</h2><span className="micro">COSTO PROMEDIO PONDERADO</span></div>
            {!analysis.positions.length ? <div className="small-empty">Sin operaciones normalizadas. Importa un ledger documentado; no deducimos swaps de simples transferencias.</div> : analysis.positions.map(p => {
              const key = `${p.token}-${p.cycle}`;
              return <div className="position" key={key}><button className="position-heading" onClick={() => setExpanded(expanded === key ? "" : key)} aria-expanded={expanded === key}>
                <span><code>{short(p.token)}</code> · ciclo {p.cycle}<small>{p.status === "open" ? "Abierta · excluida del win rate" : p.evaluable ? "Cerrada · evaluable" : "No evaluable"}</small></span>
                <span>{money(p.realized_pnl_usd)}<small>ROI realizado: {p.realized_roi_pct == null ? "Desconocido" : Number(p.realized_roi_pct).toFixed(2) + "%"} {expanded === key ? "−" : "+"}</small></span></button>
                {expanded === key && <div className="fill-list">{p.issues.length > 0 && <p className="risk">Exclusiones: {p.issues.join(", ")}</p>}{p.fills.map((f,i) => <div className="fill" key={i}><span className="badge">{f.kind}</span><span>{date(f.timestamp)}<small>{f.quantity} tokens · {money(f.gross_usd)} · fees {money(f.fees_usd)}</small></span>
                  <span><a href={f.transaction_url} target="_blank" rel="noreferrer">Transacción ↗</a><small><a href={f.source_url} target="_blank" rel="noreferrer">Fuente declarada ↗</a></small></span><span className="micro">{f.evidence.movement || "unverified"}</span></div>)}</div>}
              </div>;
            })}
          </section>}
        </>}

        {tab === "rankings" && <section className="panel"><div className="section-heading"><h2>Ranking de evidencia validada</h2><span className="badge">{rankings.length} RESULTADOS</span></div>
          <p className="muted">No utiliza el PnL reportado por FOMO para clasificar. Se exige historial normalizado completo y prueba de costos, ventas y capitalización de entrada.</p>
          {!rankings.length ? <div className="empty"><div className="empty-icon">↗</div><h2>Aún no hay traders que superen la verificación.</h2><p>Una clasificación vacía es el resultado correcto cuando falta evidencia. Descubre candidatos y examina su historial desde el explorador.</p><button className="secondary" onClick={() => setTab("overview")}>Investigar wallets →</button></div> : rankings.map(r => <button className="ranking-row" key={r.address} onClick={() => {setSelected(r.address); setTab("overview");}}>{r.label || short(r.address)}<span>{pct(r.metrics.win_rate)} · {r.metrics.evaluable_positions} posiciones</span></button>)}
        </section>}

        {tab === "sources" && <div className="sources-grid"><section className="panel"><div className="section-heading"><h2>Fuentes disponibles</h2><span className="badge">SIN SCRAPING PRIVADO</span></div>
          <article className="source-card"><span className="source-icon">◎</span><div><h3>Solana JSON-RPC</h3><span className="badge green">Público · sin clave</span><p>Firmas, transacciones finalizadas, fees de red y balances de tokens. No proporciona por sí solo precios históricos USD, market cap de entrada ni un historial de swaps normalizado.</p><a href="https://solana.com/docs/rpc/http/gettransaction" target="_blank" rel="noreferrer">Documentación oficial ↗</a></div></article>
          <article className="source-card"><span className="source-icon">f.</span><div><h3>FOMO API · proveedor independiente</h3><span className="badge warning">{config?.fomo_api_configured ? "Clave configurada" : "Requiere FOMO_API_KEY"}</span><p>Permite resolver usuarios a wallets. Su asociación es declarada y su historial de posiciones es incompleto. No implica integración oficial con fomo.family.</p><a href="https://fomoapi.io/docs" target="_blank" rel="noreferrer">Documentación ↗</a><span> · </span><a href="https://fomoapi.io/terms" target="_blank" rel="noreferrer">Condiciones ↗</a>
            <form className="inline-form" onSubmit={e => {e.preventDefault(); action("resolve", async () => {const w = await api<Wallet>("/integrations/fomo/resolve",post({handle})); await reloadWallets(); setSelected(w.address); setTab("overview"); setNotice("Asociación de identidad declarada por el proveedor, pendiente de verificación independiente."); });}}>
              <label>Usuario de FOMO<input placeholder="@usuario" value={handle} required onChange={e => setHandle(e.target.value)}/></label><button className="secondary" disabled={!!busy || !config?.fomo_api_configured}>Resolver usuario</button></form></div></article>
          <article className="source-card"><span className="source-icon">⌕</span><div><h3>Descubrimiento por semilla</h3><p>Consulta hasta tres transacciones de un mint y añade sus firmantes como candidatos sin validar. Incluye tokens fallidos y wallets manuales en tu investigación; esta muestra no demuestra habilidad ni cobertura completa.</p>
            <form className="inline-form" onSubmit={e => {e.preventDefault(); action("discover", async () => {const r = await api<{candidates:string[]}>("/discovery",post({token:seed,limit:3})); await reloadWallets(); setNotice(`${r.candidates.length} candidatos añadidos. No son ganadores verificados.`); });}}>
              <label>Mint Solana<input required placeholder="Contrato público del token" value={seed} onChange={e => setSeed(e.target.value)}/></label><button className="secondary" disabled={!!busy}>Buscar candidatos</button></form></div></article>
        </section><section className="panel methodology"><h2>Cómo se evalúa</h2><ol><li><strong>Reconstrucción cronológica</strong><p>Costo promedio ponderado. Fees de compra se incorporan al costo; fees de venta se restan de lo recuperado.</p></li>
          <li><strong>Solo beneficios realizados</strong><p>Las ventas parciales mantienen el costo remanente. Una posición cuenta al cerrar el ciclo completo. Las recompras abren nuevos ciclos.</p></li>
          <li><strong>Confianza según muestra</strong><p>Wilson al 95% sobre resultados agregados por token. Recomprar el mismo mint no crea nuevas observaciones independientes. Puede persistir correlación entre tokens.</p></li>
          <li><strong>Datos desconocidos, visibles</strong><p>Importar una URL o una firma no valida precios ni costos. Una coincidencia de balance solo verifica el movimiento. Transfers y costos faltantes impiden evaluar la posición.</p></li>
          <li><strong>Score separado del riesgo de copia</strong><p>Pesos 25/20/15/15/10/10/5. Sin backtest prospectivo, replicabilidad aporta cero. El score exploratorio nunca se presenta como validado.</p></li></ol>
          <div className="evidence-box"><strong>Alcance de esta fase</strong><p>Ingesta real, ledger, análisis, snapshots, candidatos y watchlist. Los precios históricos verificados, alertas continuas, convergencia y backtesting quedan para las siguientes fases.</p></div>
        </section></div>}
        <footer><span>FOMO SMART MONEY RESEARCH <span className="slash">/</span> Datos con procedencia, resultados con límites.</span><span>Horario: Lima · UTC−5</span></footer>
      </div>
    </main>
  </div>;
}
