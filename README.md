# FOMO Smart Money Research

Tracker independiente de investigación para Solana. No ejecuta operaciones ni custodia fondos.
Primera fase funcional: wallets manuales, descubrimiento acotado por mint, ingesta RPC real,
evidencia de movimientos, ledger importable, reconstrucción de posiciones, métricas conservadoras,
snapshots reproducibles y dashboard. No incluye datos de demostración en la aplicación.

## Inicio rápido

Requiere Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22.13+ o 24 y npm.
Usa el checkout existente; cada tarea cloud ya está aislada y no necesita otro worktree.

```bash
cd backend
uv sync --frozen --group dev
uv run uvicorn tracker.main:app --host 127.0.0.1 --port 8000
```

En otra terminal, el worker que ejecuta las sincronizaciones en segundo plano (uno solo por base de datos):

```bash
cd backend
uv run python -m tracker.worker
```

Para uso local en un solo proceso, `TRACKER_EMBEDDED_WORKER=1` arranca el worker como hilo dentro de la API.

En otra terminal:

```bash
cd frontend
npm ci
npm run dev
```

El frontend escucha en el puerto 3000 y comunica con FastAPI mediante su proxy de servidor.
Los datos locales se guardan en `backend/.data/tracker.db` (SQLite, ignorado en Git).
La API OpenAPI está en el puerto 8000, ruta `/docs`. `GET /api/health` comprueba la base de datos.
Los procesos de desarrollo deben reiniciarse en nuevas máquinas: no se asume que sobrevivan a un snapshot.

## Filtros iniciales

| Criterio | Valor por defecto |
| --- | --- |
| Capitalización máxima de **todas** las compras del ciclo | USD 250.000, inclusive |
| Gran ganador, ROI **neto realizado** | ≥ +500% = 6× del capital |
| Win rate histórico | ≥ 60% observado **y** límite inferior Wilson 95% ≥ 60% |
| Posiciones cerradas evaluables | ≥ 30 (un resto ≤ 0,01% del máximo del ciclo cuenta como cerrado, con ese polvo a valor cero) |
| Ventana de cierres | Últimos 90 días |
| Evidencia | Swaps, USD, fees, market cap histórico e historial completo verificados |

El umbral del win rate conservador es configurable. Wilson se calcula sobre resultados agregados
por mint, para que recomprar el mismo token no infle la muestra independiente.
Con 18 aciertos de 30, el 60% observado **no** pasa el criterio conservador (límite inferior ≈42,3%).
La correlación entre tokens aún puede reducir la independencia. Ver [método](docs/methodology.md).

## Uso

1. Añade una wallet pública en el explorador y pulsa **Sincronizar on-chain**.
2. **Sincronizar on-chain** encola un trabajo en segundo plano (`POST /api/wallets/{wallet}/jobs`). El worker
   lo ejecuta por rondas (por defecto 5 páginas × 50 firmas) y encola la siguiente ronda mientras falten
   firmas o precios (`until_complete`, máx. `max_rounds` = 30), de modo que un historial largo se completa
   solo y se intercala con otras wallets. El dashboard muestra la ronda y el progreso, permite cancelar
   (se detiene tras la transacción en curso, sin perder lo guardado) y avisa si no hay worker activo.
   Los errores del proveedor se reintentan con espera exponencial (30 s, 60 s, …; 5 intentos); un trabajo
   cuyo worker deja de latir 10 min vuelve a la cola. Las wallets de la **watchlist** se resincronizan cada
   `TRACKER_REFRESH_MINUTES` (30 por defecto; 0 lo desactiva). `GET /api/jobs`, `GET /api/jobs/{id}`,
   `POST /api/jobs/{id}/cancel` y `GET /api/worker` exponen la cola. `POST /api/wallets/{wallet}/sync` sigue
   disponible como ejecución síncrona y acotada (scripts, pruebas) y se registra como trabajo. Mientras una
   wallet se sincroniza, normalizar, valorar o verificar esa wallet responde 409.
   La ingesta es **incremental**: cada wallet guarda cursores (`head`, `tail`) y la siguiente
   sincronización continúa donde terminó la anterior. Primero recupera firmas más nuevas que `head`
   (si se interrumpe, retoma ese hueco antes de avanzar) y después amplía hacia atrás desde `tail`
   hasta el inicio de la ventana o del historial. Las transacciones ya almacenadas no se vuelven a
   descargar con `getTransaction`, tampoco las guardadas por **Verificar ledger**. Un listado vacío
   no consume el presupuesto de páginas. `{"revalidate": true}` vuelve a descargar desde la firma
   más reciente y compara el SHA-256 con la evidencia original, sin mover los cursores.
3. Revisa **Operaciones on-chain**: cada transacción guardada se normaliza sin llamadas al RPC
   (`tracker/normalize.py`) en compras, ventas y transferencias, con su contrapartida en SOL, USDC o USDT
   y el precio efectivo. El tramo SOL suma los lamports de la wallet y de sus cuentas de token
   (así el rent de sus propias cuentas y el wSOL se compensan) y separa la fee de red. Una operación es
   `verified` solo si se ejecutó un programa reconocido (Pump.fun, PumpSwap, Raydium AMM v4/CPMM/CLMM/
   LaunchLab o Jupiter v6), la wallet firmó y hubo exactamente un token contra una contrapartida.
   Tras cada sync, las compras y ventas se valoran en USD (`tracker/pricing.py`): importe en SOL × SOL/USD
   de la vela de 1 minuto de Coinbase que contiene el bloque (o la más cercana a ≤5 min); USDC y USDT a la
   par. El market cap de entrada solo es `verified` para mints de pump.fun (su origen lo prueba cualquier
   operación guardada contra la bonding curve): precio USD × 1.000 millones, el suministro fijo con que se
   crean, que es un límite superior porque las quemas solo lo reducen. El origen también se prueba si existe
   la cuenta de bonding curve del mint (PDA `["bonding-curve", mint]` del programa de pump.fun), así que los
   tokens graduados a PumpSwap o Raydium cuentan aunque la wallet no operara en la curva. Para el resto, la
   sync lee cada mint (`getMultipleAccounts`, caché 24 h): sin autoridad de emisión, el market cap se
   **estima** con el suministro actual (`estimated`, «≈» en el dashboard; puede subestimarse si hubo quemas
   después, por eso el ranking no lo acepta); con autoridad de emisión activa queda `unknown`.
   Las velas se guardan en caché; `POST /api/wallets/{wallet}/price` completa lo pendiente (máx. 20
   peticiones por llamada). Estas operaciones alimentan el análisis en USD y el ranking. La vista en SOL
   (`GET /api/wallets/{wallet}/chain-positions`) sigue disponible. `POST /api/wallets/{wallet}/normalize`
   reconstruye las operaciones desde la evidencia guardada; al cambiar la versión del normalizador se
   reconstruyen en la siguiente sync.
4. **Posiciones abiertas** (`GET /api/wallets/{wallet}/open-positions`): cantidad, costo, precio actual, valor,
   PnL no realizado, liquidez y riesgos del token (autoridad de congelación o de emisión activa, extensiones
   Token-2022 restrictivas, liquidez < USD 10.000). Precio del par más líquido en DEX Screener, caché de 1 min;
   si la fuente falla se muestran las últimas cotizaciones con aviso. Es precio de mercado, no de ejecución, y
   no entra en snapshots ni ranking. `GET /api/tokens/{mint}` devuelve los datos guardados de un token.
5. Opcional: importa un ledger JSON documentado. Si la cadena ya aporta el evento de la misma firma y mint,
   prevalece el de la cadena.
   La importación es una declaración externa, **no** evidencia verificada por sí misma.
6. **Verificar ledger** comprueba fecha y delta neto del token contra el RPC. No valida
   automáticamente que fuera un swap ni sus precios históricos USD o capitalización.
7. Examina métricas exploratorias, exclusiones y snapshot; añade wallets a la watchlist.

Formato del ledger (números decimales como strings; ver modelo `LedgerImport` en OpenAPI):

```text
{"events": [{
  "signature": "<firma base58 real de 64 bytes>",
  "event_index": 0,
  "token": "<mint Solana real de 32 bytes>",
  "timestamp": "<fecha real ISO-8601 con zona horaria>",
  "kind": "buy",
  "quantity": "<cantidad positiva>",
  "gross_usd": "<importe documentado, o null si no se conoce>",
  "fees_usd": "<costo total documentado, o null>",
  "market_cap_usd": "<capitalización circulante a esa fecha, o null>",
  "source_url": "<referencia HTTPS pública sin credenciales ni query>"
}]}
```

El bloque anterior es una **plantilla**, no datos de operaciones reales. `kind` admite `buy`,
`sell`, `transfer_in` y `transfer_out`. Gross y fees son importes totales, no por token.
`fees_usd` incluye fees de red y trading no incorporados ya en el efectivo del swap.
No restar slippage nuevamente si ya está reflejado en el efectivo recuperado.
Campos opcionales `fdv_usd` y `liquidity_usd` se conservan como declaraciones, sin convertirlos en market cap.

## Fuentes y credenciales

El RPC público funciona sin clave. Alternativamente configura `SOLANA_RPC_URL` con un proveedor HTTPS
de archivo autorizado. Los límites y la retención del proveedor condicionan la cobertura.

`fomoapi.io` es **independiente, no oficial**. El adaptador implementa solamente el método documentado
`GET /v2/users/{handle}` para resolver candidatos. Requiere `FOMO_API_KEY` solo en el backend.
No usa cookies, endpoints privados, extracción de FOMO ni exportación de claves de wallets.
Configura la clave mediante los secretos del entorno; nunca la incluyas en Git, el frontend o el chat.
En el entorno cloud, su destino HTTPS es `api.fomoapi.io`. La presencia de una clave no garantiza
créditos ni permisos. No se ha probado una llamada autenticada sin una clave real.

La documentación del proveedor declara historial incompleto (`complete: false`) y PnL no reconciliado.
Por ello **ningún dato importado o identidad declarada se convierte automáticamente en un trader validado**.
El ranking exige historial completo de la ventana: el análisis se calcula a la fecha del último escaneo
completo (`caught_up_at`) y la sync debe cubrir al menos los días de la ventana analizada. Hoy solo las
entradas en mints de pump.fun tienen market cap verificado; el resto deja la wallet fuera del ranking.
Un ranking vacío no significa que no existan traders rentables.
Ver [auditoría y fuentes](docs/audit.md) y [alcance pendiente](docs/roadmap.md).

Variables opcionales:

| Nombre | Uso |
| --- | --- |
| `DATABASE_URL` | Por defecto SQLite; PostgreSQL usa `postgresql+psycopg://…` |
| `SOLANA_RPC_URL` | RPC HTTPS, por defecto `https://api.mainnet-beta.solana.com` |
| (sin variable) | Precios: `https://api.exchange.coinbase.com`, público y sin clave |
| (sin variable) | Cotizaciones actuales: `https://api.dexscreener.com`, público y sin clave |
| `FOMO_API_KEY` | Bearer para `api.fomoapi.io`, solo servidor |
| `BACKEND_URL` | En Next, por defecto `http://127.0.0.1:8000` |
| `TRACKER_ACCESS_TOKEN` | Opcional: protege la API; Next lo adjunta del lado servidor |
| `CORS_ORIGINS` | Orígenes locales permitidos, separados por comas |
| `TRACKER_REFRESH_MINUTES` | Worker: intervalo de resincronización de la watchlist (30; 0 desactiva) |
| `TRACKER_EMBEDDED_WORKER` | `1` ejecuta el worker como hilo de la API (solo uso local) |

La aplicación está diseñada para desarrollo local de un solo investigador. El proxy web no implementa
autenticación de usuarios: no exponerlo públicamente sin añadir login, autorización, TLS y límites por usuario.
Ejecutar FastAPI con un solo proceso y un único worker de fondo: el bloqueo de escrituras y el limitador
RPC son locales a cada proceso. Con SQLite, API y worker comparten el archivo en modo WAL con espera de bloqueo.

## Pruebas y compilación

```bash
cd backend
uv run pytest -q
uv run ruff check .
cd ../frontend
npm run build
npm run typecheck
# Con FastAPI y Next ya levantados; usa Chromium instalado o PLAYWRIGHT_CHROMIUM_PATH.
npm run test:e2e
```

Los fixtures unitarios son **sintéticos y exclusivos de pruebas**; nunca se cargan en la aplicación.
Para una prueba real de RPC, sin inventar valoraciones:

```bash
cd backend
uv run python ../scripts/live_smoke.py
```

La prueba real consulta una transacción pública, ingiere su wallet en una base SQLite temporal,
comprueba deduplicación y confirma que no se invente un score verificado. No demuestra 90 días de historial
ni 30 posiciones ganadoras. [Evidencia de validación](docs/validation.md).

## Docker Compose

`compose.yaml` define PostgreSQL, API, worker y frontend. Elige una contraseña local y mantenla fuera de Git:

```bash
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
docker compose up --build -d
```

Puertos publicados solo en loopback: frontend 3000, API 8000. PostgreSQL usa un volumen persistente.
Para un entorno cloud con proxy/CA, usa también `compose.cloud.yaml`; monta el bundle público de CA
mediante secretos BuildKit y de runtime, sin guardarlo en las capas de imagen. Conserva los proxies
de Docker del entorno. No desactives TLS ni la validación de hashes.

La publicación del entorno cloud y la fusión en GitHub son operaciones independientes.
