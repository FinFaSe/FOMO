"use client";

import { useCallback, useEffect, useState } from "react";
import type { AlertItem, AlertList, Analysis, BacktestResult, Candidate, ChainPositions, ChainTrade, Config, Filters, Job, Observation, OpenPositions, ProspectiveReport, Seed, Wallet, WalletLinks, WorkerStatus } from "./types";

const initial: Filters = { max_entry_market_cap_usd: "250000", min_realized_roi_pct: "500",
  min_win_rate: 0.6, min_evaluable_positions: 30, analysis_days: 90, conservative_win_rate: true };
const short = (s: string) => `${s.slice(0, 5)}…${s.slice(-5)}`;
const money = (s: string | number | null | undefined) => s == null ? "Sin datos" :
  new Intl.NumberFormat("es-PE", { style: "currency", currency: "USD", maximumFractionDigits: 2 }).format(Number(s));
const sol = (s: string | null | undefined) => s == null ? "Sin datos" : `${Number(s).toLocaleString("es-PE", { maximumFractionDigits: 4 })} SOL`;
const roi = (s: string | null | undefined) => s == null ? "Desconocido" : `${Number(s).toFixed(1)}%`;
const ACTIVE_JOB = ["queued", "running", "cancel_requested"];
const riskNames: Record<string, string> = { freeze_authority_active: "Puede congelar cuentas", mint_authority_active: "Puede emitir más tokens",
  token2022_restrictive_extension: "Extensión Token-2022 restrictiva", low_liquidity: "Liquidez < USD 10.000" };
const alertNames: Record<string, string> = { buy: "Compra", sell: "Venta", convergence: "Convergencia" };
const alertToken = (a: AlertItem) => a.details.symbol || short(a.mint);
const alertTitle = (a: AlertItem) => a.kind === "convergence" ? `Convergencia en ${alertToken(a)}` : `${alertNames[a.kind]}: ${a.details.label || short(a.wallet || "")}`;
const alertBody = (a: AlertItem) => a.kind === "convergence"
  ? `${a.details.entities} grupos de tu watchlist compraron ${alertToken(a)} en ${a.details.window_minutes} min`
  : `${alertToken(a)} · ${a.details.usd ? money(a.details.usd) : `${a.details.quote_amount ?? "?"} ${a.details.quote_asset ?? ""}`}${a.details.market_cap_usd ? ` · MC ${a.details.market_cap_status === "estimated" ? "≈" : ""}${money(a.details.market_cap_usd)}` : ""}`;
const ago = (s: string | null) => { if (!s) return "—"; const m = Math.round((Date.now() - new Date(s).getTime()) / 60000); return m < 1 ? "ahora" : m < 60 ? `hace ${m} min` : `hace ${Math.round(m / 60)} h`; };
const jobStates: Record<string, string> = { queued: "En cola", running: "Sincronizando", cancel_requested: "Cancelando…",
  done: "Completado", failed: "Falló", cancelled: "Cancelado" };
const seedStates: Record<string, string> = { queued: "En cola", scanning: "Escaneando", done: "Escaneada", failed: "Falló" };
const flagNames: Record<string, string> = { creator: "Creador", launch_tx: "Compra en la creación", launch_slot: "Mismo bloque del lanzamiento",
  funded_by_creator: "Financiada por el creador", funded_creator: "Financió al creador", shares_funder_with_creator: "Mismo financiador que el creador",
  linked_to_creator: "Vinculada al creador" };
const botNames: Record<string, string> = { likely_bot: "Probable bot / MEV", possible_bot: "Posible bot", none: "Sin señales de bot" };
const linkNames: Record<string, string> = { same_funder: "mismo financiador", funded_by: "financiada por", co_buy: "compran juntas" };
const basisNames: Record<string, string> = { scanned_trades: "operaciones escaneadas", current_quote: "cotización actual", declared: "declarado" };
const kinds: Record<string, string> = { buy: "Compra", sell: "Venta", transfer_in: "Entrada", transfer_out: "Salida" };
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
  bot_or_mev_behavior: "Comportamiento de bot o MEV: no es una señal copiable",
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
  const [chain, setChain] = useState<ChainPositions | null>(null);
  const [trades, setTrades] = useState<ChainTrade[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [openPositions, setOpenPositions] = useState<OpenPositions | null>(null);
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [jobsVersion, setJobsVersion] = useState(0);
  const [verifyOffset, setVerifyOffset] = useState(0);
  const [rankings, setRankings] = useState<{ address: string; label: string; metrics: Analysis["metrics"]; entity: { size: number } | null }[]>([]);
  const [links, setLinks] = useState<WalletLinks | null>(null);
  const [alertList, setAlertList] = useState<AlertList | null>(null);
  const [unread, setUnread] = useState(0);
  const [alertKind, setAlertKind] = useState("");
  const [desktopAlerts, setDesktopAlerts] = useState(false);
  const [backtest, setBacktest] = useState<BacktestResult | null>(null);
  const [backtestSize, setBacktestSize] = useState("0.5");
  const [backtestSlippage, setBacktestSlippage] = useState("1");
  const [prospective, setProspective] = useState<ProspectiveReport | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [address, setAddress] = useState("");
  const [label, setLabel] = useState("");
  const [handle, setHandle] = useState("");
  const [seed, setSeed] = useState("");
  const [seedLabel, setSeedLabel] = useState("");
  const [seedPeak, setSeedPeak] = useState("");
  const [seedPeakSource, setSeedPeakSource] = useState("");
  const [seeds, setSeeds] = useState<Seed[]>([]);
  const [candidateList, setCandidateList] = useState<{ items: Candidate[]; total: number; seeds: number } | null>(null);
  const [discoveryOptions, setDiscoveryOptions] = useState({ multiple: 10, min_seeds: 1, include_flagged: false });
  const [discoveryVersion, setDiscoveryVersion] = useState(0);
  const [expanded, setExpanded] = useState("");
  const wallet = wallets.find(w => w.address === selected);
  const activeJob = jobs.find(j => ACTIVE_JOB.includes(j.state));
  const lastJob = jobs[0];

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
    setAnalysis(null); setObservations([]); setObservationTotal(0); setChain(null); setTrades([]); setOpenPositions(null); setLinks(null); setBacktest(null);
    if (selected) {
      // Current quotes come from an external source: their failure must not hide the rest of the profile.
      api<OpenPositions>(`/wallets/${selected}/open-positions`).then(op => { if (active) setOpenPositions(op); }).catch(() => {});
      api<WalletLinks>(`/wallets/${selected}/links`).then(l => { if (active) setLinks(l); }).catch(() => {});
      Promise.all([api<Analysis>(`/wallets/${selected}/analysis?${query(filters)}`),
        api<{ items: Observation[]; total: number }>(`/wallets/${selected}/observations?limit=20&offset=${observationOffset}`),
        api<ChainPositions>(`/wallets/${selected}/chain-positions?days=${filters.analysis_days}`),
        api<{ items: ChainTrade[] }>(`/wallets/${selected}/trades?limit=20`)])
        .then(([a, o, cp, t]) => { if (active) { setAnalysis(a); setObservations(o.items); setObservationTotal(o.total); setChain(cp); setTrades(t.items); } })
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

  // Worker status for the top bar, also when no wallet is selected.
  useEffect(() => {
    let active = true;
    const load = () => api<WorkerStatus>("/worker").then(w => { if (active) setWorker(w); }).catch(() => {});
    load(); const timer = setInterval(load, 15000);
    return () => { active = false; clearInterval(timer); };
  }, []);

  // Poll the selected wallet's jobs: fast while one is active, slow otherwise. Each finished round
  // refreshes the wallet so the analysis grows as the background sync advances.
  useEffect(() => {
    let active = true; let timer: ReturnType<typeof setTimeout> | undefined;
    let lastFinished: number | null = null;
    setJobs([]);
    async function tick() {
      if (!selected) return;
      try {
        const r = await api<{ items: Job[]; worker: WorkerStatus }>(`/jobs?wallet=${selected}&limit=5`);
        if (!active) return;
        setJobs(r.items); setWorker(r.worker);
        const finished = r.items.find(j => !ACTIVE_JOB.includes(j.state))?.id ?? null;
        if (lastFinished !== null && finished !== lastFinished) await reloadWallets();
        lastFinished = finished;
        timer = setTimeout(tick, r.items.some(j => ACTIVE_JOB.includes(j.state)) ? 2000 : 15000);
      } catch { if (active) timer = setTimeout(tick, 15000); }
    }
    tick();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [selected, jobsVersion, reloadWallets]);

  // Discovery tab: seeds (polled while a scan is pending) and cross-seed candidates.
  useEffect(() => {
    if (tab !== "discovery") return;
    let active = true; let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const q = new URLSearchParams({ multiple: String(discoveryOptions.multiple), min_seeds: String(discoveryOptions.min_seeds),
          include_flagged: String(discoveryOptions.include_flagged) });
        const [sd, cd] = await Promise.all([api<{ items: Seed[] }>("/discovery/seeds"),
          api<{ items: Candidate[]; total: number; seeds: number }>(`/discovery/candidates?${q}`)]);
        if (!active) return;
        setSeeds(sd.items); setCandidateList(cd);
        if (sd.items.some(x => x.state === "queued" || x.state === "scanning")) timer = setTimeout(load, 4000);
      } catch (e) { if (active) setError(e instanceof Error ? e.message : "Error inesperado"); }
    }
    load();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [tab, discoveryOptions, discoveryVersion]);

  async function addSeed(e: React.FormEvent) {
    e.preventDefault();
    await action("seed", async () => {
      const body: Record<string, string> = { mint: seed.trim(), label: seedLabel.trim() };
      if (seedPeak) { body.peak_market_cap_usd = seedPeak; if (seedPeakSource) body.peak_source_url = seedPeakSource.trim(); }
      const r = await api<{ created: boolean }>("/discovery/seeds", post(body));
      setSeed(""); setSeedLabel(""); setSeedPeak(""); setSeedPeakSource(""); setDiscoveryVersion(v => v + 1);
      setNotice(r.created ? "Semilla en cola: el worker escaneará sus primeras operaciones." : "Semilla actualizada y en cola para volver a escanear.");
    });
  }
  async function study(wallet: string) {
    await action("study", async () => {
      await api(`/discovery/candidates/${wallet}/study`, post({}));
      await reloadWallets(); setDiscoveryVersion(v => v + 1);
      setNotice("Wallet añadida al estudio y sincronización en cola. El ranking dirá si su historial completo lo confirma.");
    });
  }

  // Desktop notifications are a per-browser convenience: remembered in localStorage (which may be unavailable).
  useEffect(() => {
    try { setDesktopAlerts(localStorage.getItem("fomo-desktop-alerts") === "1" && typeof Notification !== "undefined" && Notification.permission === "granted"); } catch { /* storage blocked */ }
  }, []);
  async function toggleDesktopAlerts() {
    if (desktopAlerts) { setDesktopAlerts(false); try { localStorage.setItem("fomo-desktop-alerts", "0"); } catch { /* ignore */ } return; }
    if (typeof Notification === "undefined") { setError("Este navegador no admite notificaciones."); return; }
    const permission = await Notification.requestPermission();
    const on = permission === "granted";
    setDesktopAlerts(on);
    try { localStorage.setItem("fomo-desktop-alerts", on ? "1" : "0"); } catch { /* ignore */ }
    if (!on) setNotice("El navegador no concedió permiso para notificaciones.");
  }

  // Alerts: unread badge everywhere (and desktop notifications for new ones); full list on the Alerts tab.
  useEffect(() => {
    let active = true; let lastId = -1; let timer: ReturnType<typeof setTimeout> | undefined;
    async function load() {
      try {
        const q = new URLSearchParams({ limit: tab === "alerts" ? "100" : "20" });
        if (tab === "alerts" && alertKind) q.set("kind", alertKind);
        const r = await api<AlertList>(`/alerts?${q}`);
        if (!active) return;
        setUnread(r.unread);
        if (tab === "alerts") setAlertList(r);
        const newest = r.items[0]?.id ?? 0;
        if (lastId >= 0 && desktopAlerts) {
          r.items.filter(a => a.id > lastId && !a.read).reverse().forEach(a => {
            try { new Notification(alertTitle(a), { body: alertBody(a) }); } catch { /* ignore */ }
          });
        }
        lastId = Math.max(lastId, newest);
      } catch { /* the badge is best effort */ }
      if (active) timer = setTimeout(load, tab === "alerts" ? 4000 : 10000);
    }
    load();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [tab, alertKind, desktopAlerts]);

  async function runBacktest() {
    await action("backtest", async () => {
      setBacktest(await api<BacktestResult>("/backtest/historical", post({ wallets: [selected], size_sol: backtestSize,
        slippage_bps: String(Math.round(Number(backtestSlippage) * 100)) })));
    });
  }
  useEffect(() => {
    if (tab !== "alerts") return;
    let active = true;
    const load = () => api<ProspectiveReport>("/backtest/prospective").then(r => { if (active) setProspective(r); }).catch(() => {});
    load(); const timer = setInterval(load, 30000);
    return () => { active = false; clearInterval(timer); };
  }, [tab]);

  async function markAllRead() {
    await action("read", async () => {
      const r = await api<{ unread: number }>("/alerts/read", post({ all: true }));
      setUnread(r.unread); setAlertList(l => l ? { ...l, unread: r.unread, items: l.items.map(a => ({ ...a, read: true })) } : l);
    });
  }

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
      const r = await api<{ job: Job; created: boolean }>(`/wallets/${selected}/jobs`, post({ days: filters.analysis_days }));
      setJobsVersion(v => v + 1);
      setNotice(r.created ? "Sincronización en cola. Se ejecuta en segundo plano, por rondas, hasta completar la ventana; puedes seguir trabajando."
        : "Ya hay una sincronización en curso para esta wallet.");
    });
  }
  async function cancelJob(job: Job) {
    await action("cancel", async () => {
      await api(`/jobs/${job.id}/cancel`, post({}));
      setJobsVersion(v => v + 1);
      setNotice("Cancelación solicitada: se detiene tras la transacción en curso. Lo ya guardado se conserva.");
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
    {id: "discovery", name: "Descubrimiento", icon: "⌕"}, {id: "alerts", name: "Alertas", icon: "◉"}, {id: "watchlist", name: "Watchlist", icon: "☆"},
    {id: "sources", name: "Fuentes y método", icon: "◎"}];

  return <div className="shell">
    <aside className="sidebar">
      <a className="brand" href="/" aria-label="Inicio Smart Money"><span className="brand-mark">f.</span><div>FOMO<span>SMART MONEY RESEARCH</span></div></a>
      <div className="workspace-label">ESPACIO DE INVESTIGACIÓN</div>
      <nav aria-label="Navegación principal">{tabs.map(t => <button key={t.id} className={tab === t.id ? "nav-item active" : "nav-item"}
        onClick={() => setTab(t.id)}><span>{t.icon}</span>{t.name}{t.id === "watchlist" && <small>{wallets.filter(w => w.favorite).length}</small>}{t.id === "alerts" && unread > 0 && <small>{unread}</small>}</button>)}</nav>
      <div className="sidebar-note"><span className="status-dot" /> Solana · mainnet<div>Solo datos reales<br/>Sin ejecución de operaciones</div></div>
      <div className="independent">Herramienta de investigación independiente. Sin afiliación con fomo.family.</div>
    </aside>
    <main>
      <header className="topbar"><span>Investigación <span className="slash">/</span> {tabs.find(t => t.id === tab)?.name}</span>
        <div className="pills"><div className="network-pill" title={worker?.alive ? `Watchlist cada ${worker.workers[0]?.info?.refresh_minutes ?? "?"} min` : "Sin worker: las sincronizaciones quedan en cola"}>
          <span className={worker?.alive ? "status-dot" : "status-dot muted-dot"} />{worker?.alive ? `Worker activo${worker.queued ? ` · ${worker.queued} en cola` : ""}` : "Worker detenido"}</div>
          <div className="network-pill"><span className={config ? "status-dot" : "status-dot muted-dot"} />{config ? "Backend conectado" : "Conectando…"}</div></div></header>
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
                <div className="toolbar"><button className="primary" disabled={!!busy || !!activeJob} onClick={sync}>{activeJob ? "Sincronizando…" : busy === "sync" ? "Encolando…" : "Sincronizar on-chain"}</button>
                  <label className={`secondary file-button ${busy ? "disabled" : ""}`}>Importar ledger JSON<input aria-label="Importar ledger JSON" type="file" accept="application/json,.json" disabled={!!busy}
                    onChange={e => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ""; }}/></label>
                  <button className="quiet" disabled={!!busy} onClick={() => action("verify", async () => {
                    const r = await api<{matched_movements:number; errors:{error:string}[]; next_offset: number | null; remaining_signatures:number}>(`/wallets/${selected}/verify?offset=${verifyOffset}`, post({}));
                    setVerifyOffset(r.next_offset || 0);
                    setAnalysis(await api<Analysis>(`/wallets/${selected}/analysis?${query(filters)}`));
                    setNotice(`${r.matched_movements} movimientos coinciden con la cadena. No verifica USD ni market cap. ${r.remaining_signatures ? `${r.remaining_signatures} firmas pendientes; vuelve a pulsar para continuar. ` : ""}${r.errors.map(e => e.error).join("; ")}`);
                  })}>{verifyOffset ? "Continuar verificación ↗" : "Verificar ledger ↗"}</button>
                  <button className="quiet" disabled={!!busy} onClick={() => action("price", async () => {
                    const r = await api<{priced:number; unpriced:number; market_caps:number; price_requests:number}>(`/wallets/${selected}/price`, post({}));
                    await reloadWallets();
                    setNotice(`${r.priced} operaciones valoradas en USD, ${r.market_caps} con market cap verificado${r.unpriced ? `, ${r.unpriced} pendientes (vuelve a pulsar)` : ""}.`);
                  })}>{busy === "price" ? "Valorando…" : "Valorar en USD"}</button></div>
                {activeJob && <div className="sync-note job-note" role="status"><span><span className={activeJob.state === "queued" && !worker?.alive ? "status-dot muted-dot" : "status-dot"} />
                  {jobStates[activeJob.state]} · ronda {activeJob.round} de máx. {activeJob.params.max_rounds} · {activeJob.progress?.transactions ?? 0} transacciones nuevas en esta ronda · {activeJob.progress?.pages ?? 0} {activeJob.progress?.pages === 1 ? "página" : "páginas"}
                  {activeJob.error ? ` · reintento ${activeJob.attempts}: ${activeJob.error}` : ""}</span>
                  {activeJob.state !== "cancel_requested" && <button className="quiet" disabled={!!busy} onClick={() => cancelJob(activeJob)}>Cancelar</button>}</div>}
                {activeJob?.state === "queued" && worker && !worker.alive && <p className="risk">No hay ningún worker activo: el trabajo espera en cola. Inicia <code>uv run python -m tracker.worker</code> en <code>backend/</code>.</p>}
                {!activeJob && lastJob?.state === "failed" && <p className="risk">La última sincronización falló: {lastJob.error}</p>}
                {!activeJob && wallet.sync && <p className="sync-note">Último escaneo: {wallet.sync.state} · {observationTotal} transacciones guardadas ({wallet.sync.transactions} nuevas en la última ronda) · {wallet.sync.signature_window_complete ? "ventana de firmas escaneada" : "cobertura parcial"}. El análisis usa el estado de este escaneo.</p>}
                {analysis ? <>
                  <div className="profile-metrics">{[
                    ["PnL realizado (USD)",money(analysis.metrics.net_realized_pnl_usd)],
                    ["Win rate observado",pct(analysis.metrics.win_rate)],
                    ["Wilson 95% · por token",`${pct(analysis.metrics.win_rate_ci95[0])} – ${pct(analysis.metrics.win_rate_ci95[1])}`],
                    ["Muestra / tokens efectivos",`${analysis.metrics.evaluable_positions} / ${analysis.metrics.effective_sample_tokens}`],
                    ["Grandes ganadores tempranos",String(analysis.metrics.early_big_winners)],
                    ["Score verificado",analysis.score.verified_smart_money == null ? "No disponible" : `${analysis.score.verified_smart_money} / 100`],
                  ].map(([k,v]) => <div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
                  <div className="evidence-box">{analysis.eligible ? <p><strong>Cumple todos los criterios con evidencia verificada</strong></p> : <><strong>Lo que falta para clasificar esta wallet</strong><ul>{analysis.exclusion_reasons.map(r => <li key={r}>{reasons[r] || r}</li>)}</ul></>}
                    <span>ROI +500% = 6×. Las posiciones abiertas y las transferencias sin costo conocido no cuentan como aciertos.</span></div>
                  {analysis.behavior && <div className={`sync-note ${analysis.behavior.classification !== "none" ? "risk" : ""}`}>
                    <strong>{botNames[analysis.behavior.classification] || analysis.behavior.classification}</strong> · {analysis.behavior.same_block_round_trips} compra-venta en ≤2 bloques · {analysis.behavior.fast_round_trips} posiciones de ≤60 s ({pct(analysis.behavior.fast_round_trip_share)}) · máx. {analysis.behavior.max_trades_per_day} operaciones/día</div>}
                  {links && <div className="sync-note">{links.funding?.state === "done" ? <>Financiada por <a href={`https://solscan.io/account/${links.funding.funder}`} target="_blank" rel="noreferrer"><code>{short(links.funding.funder || "")}</code></a>{links.funding.funder_kind === "high_volume" ? " (alto volumen: exchange o servicio, no vincula)" : ""}</>
                      : links.funding?.state === "unknown" ? `Origen de fondos desconocido (${links.funding.reason})` : links.funding ? "Origen de fondos: búsqueda en cola" : "Origen de fondos sin buscar"}
                    {links.entity ? <> · <span className="risk">Grupo vinculado de {links.entity.size} wallets ({[...new Set(links.entity.links.map(l => linkNames[l.reason] || l.reason))].join(", ")}): {links.entity.members.filter(m => m !== selected).map(short).join(", ")}</span></> : " · sin wallets vinculadas conocidas"}
                    {links.funded.length > 0 && <> · financió a {links.funded.length} wallet(s) conocidas</>}</div>}
                  <h3>PnL realizado acumulado <span className="micro">{analysis.metrics.verified_data_fraction === 1 ? "VERIFICADO" : "INCLUYE DATOS DECLARADOS"} · NO ES CAPITAL TOTAL</span></h3><PnlChart points={analysis.realized_pnl_curve}/>
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
          {wallet && <section className="panel transactions"><div className="section-heading"><h2>¿Copiarla habría funcionado?</h2><span className="micro">RECONSTRUCCIÓN HISTÓRICA</span></div>
            <p className="muted">Simula copiar cada operación de esta wallet con distintos retrasos. La copia se ejecuta al primer precio observado en la base a partir de ese momento (con deslizamiento y costes); si no hay ninguno en 5 min, cuenta como «sin precio». Las ventas se copian en proporción. Retraso 0 es el precio del propio líder: un ideal inalcanzable.</p>
            <div className="inline-form">
              <label>Tamaño por compra (SOL)<input type="number" min="0.001" step="0.1" value={backtestSize} onChange={e => setBacktestSize(e.target.value)}/></label>
              <label>Deslizamiento (%)<input type="number" min="0" max="50" step="0.5" value={backtestSlippage} onChange={e => setBacktestSlippage(e.target.value)}/></label>
              <button className="secondary" disabled={!!busy} onClick={runBacktest}>{busy === "backtest" ? "Simulando…" : "Simular copia"}</button>
            </div>
            {backtest && (!backtest.leader_trades ? <div className="small-empty">Sin operaciones con contrapartida SOL en la ventana. Sincroniza la wallet primero.</div> :
              <><p className="sync-note">{backtest.leader_trades} operaciones del líder · {backtest.observations} precios observados en la base. Pocos precios observados vuelven la simulación poco representativa.</p>
              <div className="table-wrap"><table><thead><tr><th>Retraso</th><th>Compras copiadas</th><th>Sin precio</th><th>Cerradas</th><th>Win rate</th><th>PnL (SOL)</th><th>ROI copia</th><th>ROI líder</th><th>Diferencia</th><th>Abiertas</th></tr></thead><tbody>{backtest.results.map(r => <tr key={r.delay_seconds}>
                <td className="mono">{r.delay_seconds === 0 ? "0 s (ideal)" : r.delay_seconds < 60 ? `${r.delay_seconds} s` : `${r.delay_seconds / 60} min`}</td>
                <td className="mono">{r.filled_buys} / {r.buy_signals}</td>
                <td className="mono">{r.unfillable_buys + r.unfillable_sells}</td>
                <td className="mono">{r.closed_positions}</td>
                <td className="mono">{pct(r.win_rate)}</td>
                <td className="mono">{sol(r.realized_pnl_sol)}</td>
                <td className="mono">{roi(r.roi_pct)}</td>
                <td className="mono">{roi(r.leader_roi_pct)}</td>
                <td className="mono">{r.copy_gap_pct == null ? "—" : `${Number(r.copy_gap_pct) >= 0 ? "+" : ""}${Number(r.copy_gap_pct).toFixed(1)} pp`}</td>
                <td className="mono">{r.open_positions}{r.open_mark_value_sol ? <small> · {sol(r.open_mark_value_sol)}</small> : null}</td></tr>)}</tbody></table></div></>)}
          </section>}
          {wallet && openPositions && openPositions.positions.length > 0 && <section className="panel transactions"><div className="section-heading"><h2>Posiciones abiertas</h2>
              <span className="micro">PRECIO ACTUAL · {openPositions.quote_source?.replace("https://", "").toUpperCase()}</span></div>
            <p className="muted">Valoradas al precio de mercado actual (no es precio de ejecución: vender en un pool con poca liquidez movería el precio). No forman parte de los snapshots ni del ranking.</p>
            {openPositions.issues.length > 0 && <p className="risk">Cotizaciones no actualizadas: {openPositions.issues.join("; ")}. Se muestran las últimas guardadas.</p>}
            <div className="profile-metrics">{[
              ["Valor actual", money(openPositions.totals.value_usd)],
              ["Costo", money(openPositions.totals.cost_basis_usd)],
              ["PnL no realizado", money(openPositions.totals.unrealized_pnl_usd)],
              ["Posiciones valoradas", `${openPositions.totals.valued_positions} / ${openPositions.totals.open_positions}`],
            ].map(([k,v]) => <div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
            <div className="table-wrap"><table><thead><tr><th>Token</th><th>Cantidad</th><th>Costo</th><th>Precio</th><th>Valor</th><th>PnL no realizado</th><th>Liquidez</th><th>Riesgos</th><th>Cotización</th></tr></thead><tbody>{openPositions.positions.map(p => <tr key={`${p.token}-${p.cycle}`}>
              <td><a href={`https://solscan.io/token/${p.token}`} target="_blank" rel="noreferrer"><code>{p.symbol || short(p.token)} ↗</code></a></td>
              <td className="mono">{Number(p.quantity).toLocaleString("es-PE", { maximumFractionDigits: 2 })}</td>
              <td className="mono">{money(p.cost_basis_usd)}</td>
              <td className="mono">{p.price_usd == null ? "Sin cotización" : `$${Number(p.price_usd).toPrecision(4)}`}</td>
              <td className="mono">{money(p.value_usd)}</td>
              <td className="mono">{p.unrealized_pnl_usd == null ? "—" : `${money(p.unrealized_pnl_usd)} (${Number(p.unrealized_pct).toFixed(1)}%)`}</td>
              <td className="mono">{money(p.liquidity_usd)}</td>
              <td>{p.risks.length ? <small className="risk">{p.risks.map(r => riskNames[r] || r).join(" · ")}</small> : <small>—</small>}</td>
              <td><small>{p.quote_found ? `${p.dex_id ?? ""} · ${ago(p.quote_fetched_at)}` : "—"}</small></td></tr>)}</tbody></table></div>
          </section>}
          {wallet && chain && <section className="panel transactions"><div className="section-heading"><h2>Operaciones on-chain</h2><span className="micro">EN SOL · EXPLORATORIO</span></div>
            <p className="muted">Compras y ventas detectadas en las transacciones guardadas (Pump.fun, PumpSwap, Raydium, Jupiter). «Verificada» exige un programa reconocido y la firma de la wallet. Valoradas en USD con SOL/USD de Coinbase. Market cap verificado para mints de pump.fun (también tras graduarse); «≈» es una estimación con el suministro actual, que el ranking no acepta. El ranking usa la versión en USD de estas posiciones.</p>
            <div className="profile-metrics">{[
              ["PnL realizado (SOL)", sol(chain.summary.net_realized_pnl_sol)],
              ["Win rate por posición", pct(chain.summary.win_rate)],
              ["Cerradas evaluables / cerradas", `${chain.summary.evaluable_positions} / ${chain.summary.closed_positions}`],
              ["ROI agregado", roi(chain.summary.aggregate_realized_roi_pct)],
              ["ROI mediano", roi(chain.summary.median_roi_pct)],
              ["Posiciones abiertas", String(chain.summary.open_positions)],
            ].map(([k,v]) => <div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
            {!chain.positions.length ? <div className="small-empty">Sin compras ni ventas detectadas. Sincroniza la wallet para analizar sus transacciones.</div> :
              <div className="table-wrap"><table><thead><tr><th>Token</th><th>Estado</th><th>Costo</th><th>Recuperado</th><th>PnL</th><th>ROI</th><th>Observaciones</th></tr></thead><tbody>{chain.positions.map(p => <tr key={`${p.token}-${p.cycle}`}>
                <td><a href={`https://solscan.io/token/${p.token}`} target="_blank" rel="noreferrer"><code>{short(p.token)} ↗</code></a><small> · ciclo {p.cycle}</small></td>
                <td><span className="badge">{p.status === "open" ? "Abierta" : p.evaluable ? "Cerrada" : "No evaluable"}</span></td>
                <td className="mono">{p.status === "open" ? <>{sol(p.remaining_cost_sol)}<small> invertido</small></> : sol(p.realized_cost_sol)}</td>
                <td className="mono">{p.status === "open" && Number(p.realized_cost_sol) === 0 ? "—" : sol(p.realized_proceeds_sol)}</td>
                <td className="mono">{p.status === "open" && Number(p.realized_cost_sol) === 0 ? "—" : sol(p.realized_pnl_sol)}</td>
                <td className="mono">{roi(p.realized_roi_pct)}</td>
                <td>{p.issues.length ? <small className="risk">{p.issues.join(", ")}</small> : <small>—</small>}</td></tr>)}</tbody></table></div>}
            {trades.length > 0 && <><h3>Últimas operaciones <span className="micro">{chain.trades} EN TOTAL</span></h3>
              <div className="table-wrap"><table><thead><tr><th>Transacción</th><th>Fecha · Lima</th><th>Tipo</th><th>Token</th><th>Cantidad</th><th>Contrapartida</th><th>USD</th><th>Market cap</th><th>Venue</th><th>Evidencia</th></tr></thead><tbody>{trades.map(t => <tr key={`${t.signature}-${t.event_index}`}>
                <td><a href={t.transaction_url} target="_blank" rel="noreferrer"><code>{short(t.signature)} ↗</code></a></td>
                <td>{t.block_time == null ? "Desconocida" : date(new Date(t.block_time * 1000).toISOString())}</td>
                <td><span className="badge">{kinds[t.kind] || t.kind}</span></td><td><code>{short(t.mint)}</code></td>
                <td className="mono">{Number(t.token_amount).toLocaleString("es-PE")}</td>
                <td className="mono">{t.quote_amount == null ? "—" : `${Number(t.quote_amount).toLocaleString("es-PE", { maximumFractionDigits: 6 })} ${t.quote_asset}`}</td>
                <td className="mono">{t.usd?.gross_usd == null ? "—" : money(t.usd.gross_usd)}</td>
                <td className="mono" title={t.usd?.market_cap_method ?? undefined}>{t.usd?.market_cap_usd == null ? "—" : `${t.evidence.market_cap === "estimated" ? "≈ " : ""}${money(t.usd.market_cap_usd)}`}</td>
                <td>{t.venues.join(", ") || "—"}</td><td><span className="micro">{t.evidence.trade} · usd {t.evidence.usd} · cap {t.evidence.market_cap}</span></td></tr>)}</tbody></table></div></>}
          </section>}
          {analysis && <section className="panel transactions"><div className="section-heading"><h2>Posiciones reconstruidas</h2><span className="micro">COSTO PROMEDIO PONDERADO</span></div>
            {!analysis.positions.length ? <div className="small-empty">Sin posiciones todavía. Sincroniza la wallet para detectar sus operaciones on-chain, o importa un ledger documentado.</div> : analysis.positions.map(p => {
              const key = `${p.token}-${p.cycle}`;
              return <div className="position" key={key}><button className="position-heading" onClick={() => setExpanded(expanded === key ? "" : key)} aria-expanded={expanded === key}>
                <span><code>{short(p.token)}</code> · ciclo {p.cycle}<small>{p.status === "open" ? "Abierta · excluida del win rate" : p.evaluable ? "Cerrada · evaluable" : "No evaluable"}{Number(p.dust_written_off_quantity || 0) > 0 ? ` · resto de ${Number(p.dust_written_off_quantity).toLocaleString("es-PE")} tokens dado de baja como polvo` : ""}</small></span>
                <span>{money(p.realized_pnl_usd)}<small>ROI realizado: {p.realized_roi_pct == null ? "Desconocido" : Number(p.realized_roi_pct).toFixed(2) + "%"} {expanded === key ? "−" : "+"}</small></span></button>
                {expanded === key && <div className="fill-list">{p.issues.length > 0 && <p className="risk">Exclusiones: {p.issues.join(", ")}</p>}{p.fills.map((f,i) => <div className="fill" key={i}><span className="badge">{f.kind}</span><span>{date(f.timestamp)}<small>{f.quantity} tokens · {money(f.gross_usd)} · fees {money(f.fees_usd)}</small></span>
                  <span><a href={f.transaction_url} target="_blank" rel="noreferrer">Transacción ↗</a><small><a href={f.source_url} target="_blank" rel="noreferrer">Fuente declarada ↗</a></small></span><span className="micro">{f.evidence.movement || "unverified"}</span></div>)}</div>}
              </div>;
            })}
          </section>}
        </>}

        {tab === "alerts" && <section className="panel"><div className="section-heading"><h2>Alertas de la watchlist</h2><span className="badge">{unread} SIN LEER</span></div>
          <p className="muted">Compras y ventas recientes de tus wallets favoritas y convergencias: {alertList?.settings.convergence_min ?? 3} grupos distintos comprando el mismo token en {alertList?.settings.convergence_minutes ?? 60} min (las wallets vinculadas cuentan una vez). Solo alertan operaciones de los últimos {alertList?.settings.max_age_minutes ?? 10} min, para no repetir historial.</p>
          <p className="sync-note"><span className={worker?.watcher_alive ? "status-dot" : "status-dot muted-dot"} />{worker?.watcher_alive ? `Vigilante activo: revisa ${worker.watchers?.[0]?.info?.watching ?? 0} wallets cada ${worker.watchers?.[0]?.info?.interval_seconds ?? 20} s` : "Vigilante detenido: arranca el worker para alertas en tiempo casi real"} · Webhook saliente: {alertList?.webhook_configured ? "configurado" : "no configurado"}</p>
          <div className="inline-form">
            <label>Tipo<select value={alertKind} onChange={e => setAlertKind(e.target.value)}><option value="">Todas</option><option value="buy">Compras</option><option value="sell">Ventas</option><option value="convergence">Convergencias</option></select></label>
            <button className="secondary" onClick={toggleDesktopAlerts}>{desktopAlerts ? "Desactivar notificaciones del navegador" : "Activar notificaciones del navegador"}</button>
            <button className="quiet" disabled={!!busy || !unread} onClick={markAllRead}>Marcar todo como leído</button>
          </div>
          {!alertList?.items.length ? <div className="small-empty">Sin alertas todavía. Marca wallets con ☆ y mantén el worker en marcha.</div> :
            <div className="table-wrap"><table><thead><tr><th>Hora · Lima</th><th>Tipo</th><th>Wallet</th><th>Token</th><th>Importe</th><th>Market cap</th><th>Retraso</th><th></th></tr></thead><tbody>{alertList.items.map(a => <tr key={a.id} className={a.read ? "" : "unread"}>
              <td>{a.event_time ? date(a.event_time) : date(a.created_at)}</td>
              <td><span className={a.kind === "convergence" ? "badge warning" : "badge"}>{alertNames[a.kind] || a.kind}</span></td>
              <td>{a.kind === "convergence" ? <small>{a.details.participants?.map(p => p.label || short(p.wallet)).join(", ")}</small>
                : <button className="quiet" onClick={() => { setSelected(a.wallet || ""); setTab("overview"); }}>{a.details.label || short(a.wallet || "")}</button>}</td>
              <td><a href={`https://solscan.io/token/${a.mint}`} target="_blank" rel="noreferrer"><code>{alertToken(a)} ↗</code></a></td>
              <td className="mono">{a.kind === "convergence" ? `${a.details.entities} grupos` : a.details.usd ? money(a.details.usd) : `${a.details.quote_amount ?? "—"} ${a.details.quote_asset ?? ""}`}</td>
              <td className="mono">{a.details.market_cap_usd ? `${a.details.market_cap_status === "estimated" ? "≈ " : ""}${money(a.details.market_cap_usd)}` : "—"}</td>
              <td className="mono" title="Segundos entre el bloque y la alerta (detección entre paréntesis)">{a.alert_seconds == null ? "—" : `${Math.round(a.alert_seconds)} s`}{a.detection_seconds != null ? <small> ({Math.round(a.detection_seconds)} s)</small> : null}</td>
              <td>{a.transaction_url && <a href={a.transaction_url} target="_blank" rel="noreferrer">Transacción ↗</a>}</td></tr>)}</tbody></table></div>}
        </section>}

        {tab === "alerts" && <section className="panel transactions"><div className="section-heading"><h2>Seguimiento prospectivo</h2><span className="micro">SIN INFORMACIÓN FUTURA</span></div>
          <p className="muted">Cada compra o venta de la watchlist guarda la cotización real en el momento de la alerta y 5 y 60 minutos después. Aquí se mide qué habría ganado quien comprara al recibir la alerta (con 1% de deslizamiento): la prueba honesta, que gana valor a medida que se acumulan alertas.</p>
          {!prospective?.buy_alerts_with_entry_quote ? <div className="small-empty">Aún no hay alertas de compra con cotización registrada. Requiere el worker en marcha y wallets en la watchlist.</div> : <>
            <div className="profile-metrics">{[["5 min", "5m"], ["60 min", "60m"], ["Al vender el líder", "exit_on_leader_sell"]].map(([label, key]) => {
              const h = prospective.horizons[key];
              return <div key={key}><span>{label} · {h.n} casos</span><strong>{h.median_pct == null ? "Sin datos" : `${Number(h.median_pct) >= 0 ? "+" : ""}${Number(h.median_pct).toFixed(1)}%`}</strong><small className="muted">mediana · acierto {pct(h.hit_rate)}</small></div>;
            })}</div>
            <p className="sync-note">{prospective.buy_alerts_with_entry_quote} compras con cotización de entrada · retraso mediano de captura {prospective.median_quote_lateness_seconds == null ? "—" : `${Math.round(prospective.median_quote_lateness_seconds)} s`}</p>
            <div className="table-wrap"><table><thead><tr><th>Wallet</th><th>Casos a 60 min</th><th>Mediana 5 min</th><th>Mediana 60 min</th><th>Acierto 60 min</th><th>Al vender el líder</th></tr></thead><tbody>{prospective.wallets.slice(0, 20).map(w => <tr key={w.wallet}>
              <td><button className="quiet" onClick={() => { setSelected(w.wallet); setTab("overview"); }}>{w.label || short(w.wallet)}</button></td>
              <td className="mono">{w["60m"].n}</td>
              <td className="mono">{w["5m"].median_pct == null ? "—" : `${Number(w["5m"].median_pct).toFixed(1)}%`}</td>
              <td className="mono">{w["60m"].median_pct == null ? "—" : `${Number(w["60m"].median_pct).toFixed(1)}%`}</td>
              <td className="mono">{pct(w["60m"].hit_rate)}</td>
              <td className="mono">{w.exit_on_leader_sell.median_pct == null ? "—" : `${Number(w.exit_on_leader_sell.median_pct).toFixed(1)}%`}</td></tr>)}</tbody></table></div></>}
        </section>}

        {tab === "discovery" && <>
          <section className="panel"><div className="section-heading"><h2>Tokens semilla</h2><span className="badge">{seeds.length} SEMILLAS</span></div>
            <p className="muted">Se escanean las primeras operaciones de cada token (desde su creación) para ver quién compró antes. Incluye también tokens que fracasaron: si solo añades ganadores, la tasa de aciertos queda inflada.</p>
            <form className="inline-form" onSubmit={addSeed}>
              <label>Mint del token<input required placeholder="Contrato público del token" value={seed} onChange={e => setSeed(e.target.value)}/></label>
              <label>Etiqueta<input maxLength={80} placeholder="Opcional" value={seedLabel} onChange={e => setSeedLabel(e.target.value)}/></label>
              <label>Pico de market cap declarado (USD)<input type="number" min="0" placeholder="Opcional" value={seedPeak} onChange={e => setSeedPeak(e.target.value)}/></label>
              <label>Fuente del pico<input type="url" placeholder="https://… (opcional)" value={seedPeakSource} onChange={e => setSeedPeakSource(e.target.value)}/></label>
              <button className="secondary" disabled={!!busy}>{busy === "seed" ? "Añadiendo…" : "+ Añadir semilla"}</button></form>
            {seeds.some(x => x.state === "queued") && worker && !worker.alive && <p className="risk">No hay ningún worker activo: las semillas esperan en cola. Inicia <code>uv run python -m tracker.worker</code> en <code>backend/</code>.</p>}
            {!seeds.length ? <div className="small-empty">Todavía no hay semillas. Añade tokens que se multiplicaron y también algunos que fracasaron.</div> :
              <div className="table-wrap"><table><thead><tr><th>Token</th><th>Estado</th><th>Lanzamiento · Lima</th><th>Compradores tempranos</th><th>Market cap inicial</th><th>Pico (mínimo)</th><th>Múltiplo</th></tr></thead><tbody>{seeds.map(x => <tr key={x.mint}>
                <td><a href={`https://solscan.io/token/${x.mint}`} target="_blank" rel="noreferrer"><code>{x.label || short(x.mint)} ↗</code></a></td>
                <td><span className="badge">{seedStates[x.state] || x.state}</span>{x.error && <small className="risk"> {x.error}</small>}</td>
                <td>{x.launch_time ? date(x.launch_time) : "—"}</td>
                <td className="mono">{x.outcome?.early_buyers ?? "—"}</td>
                <td className="mono">{money(x.outcome?.launch_cap_usd)}</td>
                <td className="mono">{money(x.outcome?.peak_cap_lower_bound_usd)}{x.outcome?.peak_basis && <small> · {basisNames[x.outcome.peak_basis] || x.outcome.peak_basis}</small>}</td>
                <td className="mono">{x.outcome?.peak_multiple_from_launch ? `${Number(x.outcome.peak_multiple_from_launch).toFixed(1)}×` : "—"}</td></tr>)}</tbody></table></div>}
          </section>
          <section className="panel transactions"><div className="section-heading"><h2>Candidatos</h2><span className="badge">{candidateList?.total ?? 0} WALLETS</span></div>
            <p className="muted">Wallets que compraron entre los primeros en las semillas escaneadas. Un acierto es una semilla cuyo pico (límite inferior) llegó al múltiplo elegido desde la entrada de esa wallet. Son candidatas, no traders validados: «Estudiar» la sincroniza para medirla con el ranking.</p>
            <div className="inline-form">
              <label>Múltiplo para contar acierto<select value={discoveryOptions.multiple} onChange={e => setDiscoveryOptions({ ...discoveryOptions, multiple: Number(e.target.value) })}>{[3, 5, 10, 20, 50].map(n => <option key={n} value={n}>{n}×</option>)}</select></label>
              <label>Semillas mínimas<select value={discoveryOptions.min_seeds} onChange={e => setDiscoveryOptions({ ...discoveryOptions, min_seeds: Number(e.target.value) })}>{[1, 2, 3, 5].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
              <label className="checkbox"><input type="checkbox" checked={discoveryOptions.include_flagged} onChange={e => setDiscoveryOptions({ ...discoveryOptions, include_flagged: e.target.checked })}/> Incluir creadores y compras en el bloque del lanzamiento</label>
            </div>
            {!candidateList?.items.length ? <div className="small-empty">{candidateList?.seeds ? "Ninguna wallet cumple los filtros." : "Escanea al menos una semilla para ver candidatos."}</div> :
              <div className="table-wrap"><table><thead><tr><th>Wallet</th><th>Semillas</th><th>Aciertos / fallos</th><th>Wilson 95%</th><th>Minutos tras lanzamiento</th><th>Rango mediano</th><th>Copiar 30 s después</th><th>Tamaño vs curva</th><th>Marcas</th><th></th></tr></thead><tbody>{candidateList.items.map(x => <tr key={x.wallet}>
                <td><a href={`https://solscan.io/account/${x.wallet}`} target="_blank" rel="noreferrer"><code>{short(x.wallet)} ↗</code></a></td>
                <td className="mono">{x.seeds}</td>
                <td className="mono">{x.hits} / {x.misses}{x.unknown ? <small> · {x.unknown} sin dato</small> : null}{x.declared_peak_hits ? <small className="risk"> · {x.declared_peak_hits} con pico declarado</small> : null}</td>
                <td className="mono">{pct(x.hit_rate_ci95[0])} – {pct(x.hit_rate_ci95[1])}</td>
                <td className="mono">{x.median_minutes_after_launch == null ? "—" : x.median_minutes_after_launch.toFixed(1)}</td>
                <td className="mono">{x.median_rank}</td>
                <td className="mono" title="Mediana de cuánto había subido el precio 30 s después de su compra">{x.median_price_move_30s == null ? "—" : `${Number(x.median_price_move_30s) >= 0 ? "+" : ""}${(Number(x.median_price_move_30s) * 100).toFixed(0)}%`}</td>
                <td className="mono">{x.median_size_vs_curve == null ? "—" : pct(Number(x.median_size_vs_curve))}</td>
                <td>{x.entity || x.flags.length ? <small className="risk">{[...(x.entity ? [`Grupo de ${x.entity.size}`] : []), ...x.flags.map(f => flagNames[f] || f)].join(" · ")}</small> : <small>—</small>}</td>
                <td>{x.tracked ? <button className="quiet" onClick={() => { setSelected(x.wallet); setTab("overview"); }}>Ver perfil →</button> : <button className="secondary" disabled={!!busy} onClick={() => study(x.wallet)}>Estudiar</button>}</td></tr>)}</tbody></table></div>}
          </section>
        </>}

        {tab === "rankings" && <section className="panel"><div className="section-heading"><h2>Ranking de evidencia validada</h2><span className="badge">{rankings.length} RESULTADOS</span></div>
          <p className="muted">No utiliza el PnL reportado por FOMO para clasificar. Se exige historial normalizado completo y prueba de costos, ventas y capitalización de entrada.</p>
          {!rankings.length ? <div className="empty"><div className="empty-icon">↗</div><h2>Aún no hay traders que superen la verificación.</h2><p>Una clasificación vacía es el resultado correcto cuando falta evidencia. Descubre candidatos y examina su historial desde el explorador.</p><button className="secondary" onClick={() => setTab("overview")}>Investigar wallets →</button></div> : rankings.map(r => <button className="ranking-row" key={r.address} onClick={() => {setSelected(r.address); setTab("overview");}}>{r.label || short(r.address)}<span>{pct(r.metrics.win_rate)} · {r.metrics.evaluable_positions} posiciones{r.entity ? ` · vinculada con ${r.entity.size - 1} wallet(s): no son confirmaciones independientes` : ""}</span></button>)}
        </section>}

        {tab === "sources" && <div className="sources-grid"><section className="panel"><div className="section-heading"><h2>Fuentes disponibles</h2><span className="badge">SIN SCRAPING PRIVADO</span></div>
          <article className="source-card"><span className="source-icon">◎</span><div><h3>Solana JSON-RPC</h3><span className="badge green">Público · sin clave</span><p>Firmas, transacciones finalizadas, fees de red, balances de tokens y datos de cada mint (suministro, autoridades, cuenta de bonding curve de pump.fun). No proporciona por sí solo precios históricos USD, market cap de entrada ni un historial de swaps normalizado.</p><a href="https://solana.com/docs/rpc/http/gettransaction" target="_blank" rel="noreferrer">Documentación oficial ↗</a></div></article>
          <article className="source-card"><span className="source-icon">≋</span><div><h3>DEX Screener · cotizaciones actuales</h3><span className="badge green">Público · sin clave</span><p>Precio y liquidez actuales del par más profundo de cada token, solo para valorar posiciones abiertas (caché de 1 minuto). Un precio actual nunca sustituye al precio o al market cap de entrada.</p><a href="https://docs.dexscreener.com/api/reference" target="_blank" rel="noreferrer">Documentación ↗</a></div></article>
          <article className="source-card"><span className="source-icon">$</span><div><h3>Coinbase Exchange · velas SOL-USD</h3><span className="badge green">Público · sin clave</span><p>Precio SOL/USD por vela de 1 minuto en el momento de cada operación (cierre de la vela; se guarda su rango). USDC y USDT a la par. Las velas se guardan en caché y la reconstrucción no vuelve a pedirlas.</p><a href="https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles" target="_blank" rel="noreferrer">Documentación ↗</a></div></article>
          <article className="source-card"><span className="source-icon">f.</span><div><h3>FOMO API · proveedor independiente</h3><span className="badge warning">{config?.fomo_api_configured ? "Clave configurada" : "Requiere FOMO_API_KEY"}</span><p>Permite resolver usuarios a wallets. Su asociación es declarada y su historial de posiciones es incompleto. No implica integración oficial con fomo.family.</p><a href="https://fomoapi.io/docs" target="_blank" rel="noreferrer">Documentación ↗</a><span> · </span><a href="https://fomoapi.io/terms" target="_blank" rel="noreferrer">Condiciones ↗</a>
            <form className="inline-form" onSubmit={e => {e.preventDefault(); action("resolve", async () => {const w = await api<Wallet>("/integrations/fomo/resolve",post({handle})); await reloadWallets(); setSelected(w.address); setTab("overview"); setNotice("Asociación de identidad declarada por el proveedor, pendiente de verificación independiente."); });}}>
              <label>Usuario de FOMO<input placeholder="@usuario" value={handle} required onChange={e => setHandle(e.target.value)}/></label><button className="secondary" disabled={!!busy || !config?.fomo_api_configured}>Resolver usuario</button></form></div></article>
          <article className="source-card"><span className="source-icon">⌕</span><div><h3>Descubrimiento hacia atrás</h3><p>Para cada token semilla se escanea su bonding curve de pump.fun (o el pool que indiques) hasta la creación y se registran los primeros compradores con su market cap de entrada. Las wallets que entran temprano en varias semillas que luego se multiplicaron son candidatas a estudiar. Está en la pestaña «Descubrimiento».</p>
            <button className="secondary" onClick={() => setTab("discovery")}>Ir a Descubrimiento →</button></div></article>
        </section><section className="panel methodology"><h2>Cómo se evalúa</h2><ol><li><strong>Reconstrucción cronológica</strong><p>Costo promedio ponderado. Fees de compra se incorporan al costo; fees de venta se restan de lo recuperado.</p></li>
          <li><strong>Solo beneficios realizados</strong><p>Las ventas parciales mantienen el costo remanente. Una posición cuenta al cerrar el ciclo completo. Las recompras abren nuevos ciclos.</p></li>
          <li><strong>Confianza según muestra</strong><p>Wilson al 95% sobre resultados agregados por token. Recomprar el mismo mint no crea nuevas observaciones independientes. Puede persistir correlación entre tokens.</p></li>
          <li><strong>Datos desconocidos, visibles</strong><p>Importar una URL o una firma no valida precios ni costos. Una coincidencia de balance solo verifica el movimiento. Transfers y costos faltantes impiden evaluar la posición.</p></li>
          <li><strong>Score separado del riesgo de copia</strong><p>Pesos 25/20/15/15/10/10/5. Sin backtest prospectivo, replicabilidad aporta cero. El score exploratorio nunca se presenta como validado.</p></li></ol>
          <div className="evidence-box"><strong>Alcance de esta fase</strong><p>Ingesta incremental, swaps normalizados, valoración USD por vela de 1 minuto, market cap verificado para mints de pump.fun (estimado para el resto sin autoridad de emisión), posiciones abiertas a precio actual, sincronización en segundo plano, análisis, snapshots, candidatos y watchlist. Alertas en tiempo real, convergencia y backtesting quedan para las siguientes fases.</p></div>
        </section></div>}
        <footer><span>FOMO SMART MONEY RESEARCH <span className="slash">/</span> Datos con procedencia, resultados con límites.</span><span>Horario: Lima · UTC−5</span></footer>
      </div>
    </main>
  </div>;
}
