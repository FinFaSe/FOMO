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

### Migraciones de base de datos

El esquema se gestiona con **Alembic** (`backend/tracker/migrations`). La API y el worker actualizan la base a la
última revisión al arrancar, bajo un bloqueo entre procesos (advisory lock en PostgreSQL, archivo de bloqueo junto
al SQLite), así que no migran a la vez. Una base creada antes de Alembic (por el código importado o por versiones
intermedias) se marca en la revisión base y se actualiza **conservando sus datos**.

```bash
cd backend
uv run python -m tracker.migrate current   # revisión actual y la esperada
uv run python -m tracker.migrate upgrade   # actualizar a mano (también dentro del contenedor)
```

Con `TRACKER_AUTO_MIGRATE=0` la app no migra sola y se niega a arrancar si la base no está al día (útil cuando las
migraciones se ejecutan como paso de despliegue). Haz copia de seguridad antes de migrar datos que te importen.

Para cambiar el esquema: modifica los modelos en `tracker/db.py` y genera una revisión nueva, revísala y súbela
junto al cambio. `tests/test_migrations.py` falla si los modelos y las migraciones no coinciden.

```bash
cd backend
uv run alembic revision --autogenerate -m "describe el cambio"
uv run alembic check   # sin cambios pendientes
```
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
5. **Descubrimiento** (pestaña propia): añade tokens semilla —ganadores y también fracasos— con
   `POST /api/discovery/seeds`. El worker (o `POST /api/discovery/seeds/{mint}/scan`) lista las firmas de su
   bonding curve de pump.fun hasta la creación (o del pool indicado; reanuda si se agota el presupuesto de
   páginas), descarga las primeras operaciones (100 por defecto) y registra cada comprador con rango, minutos
   tras el lanzamiento, market cap de entrada y marcas (creador, compra en la creación, mismo bloque del
   lanzamiento). El pico de cada semilla es un **límite inferior**: máximo entre las operaciones escaneadas, la
   cotización actual y un pico declarado opcional (marcado como tal). `GET /api/discovery/candidates` cruza las
   semillas: una wallet acierta una semilla si ese pico llega al múltiplo elegido (10× por defecto) desde su
   entrada; se ordena por aciertos y límite inferior de Wilson y por defecto excluye las wallets marcadas.
   «Estudiar» (`POST /api/discovery/candidates/{wallet}/study`) la añade y encola su sincronización.
   `POST /api/discovery` (firmantes recientes) ahora escanea la bonding curve cuando existe.
6. **Bots, insiders y vínculos**:
   - El análisis calcula señales de comportamiento (compra y venta del mismo token en ≤ 2 bloques, posiciones
     de ≤ 60 s, operaciones por día). «Probable bot / MEV» excluye la wallet del ranking (`bot_or_mev_behavior`).
   - Las propinas a Jito que paga la wallet se separan del importe del swap (`tip_sol`, se suman a las fees).
   - **Origen de fondos** (`wallet_funding`): quién envió a cada dirección su primer SOL, buscando en sus
     transacciones más antiguas. El worker lo hace en segundo plano para las wallets en estudio, el creador y
     los primeros compradores de cada semilla; `POST /api/funding/{address}/lookup` lo hace al momento. Un
     financiador con 1.000 firmas en menos de un día (exchange, router, bot) no vincula.
   - **Entidades** (`GET /api/entities`, `GET /api/wallets/{wallet}/links`): mismo financiador, financiada por
     otra dirección conocida, o compras en el mismo bloque en ≥ 2 semillas. Son indicios, no prueba. El ranking
     señala las wallets vinculadas y los candidatos muestran su grupo.
   - **Insiders**: un candidato financiado por el creador, que financió al creador, que comparte financiador
     con él o que está vinculado a él queda marcado y excluido por defecto.
   - **¿Se puede copiar?**: para cada compra temprana se mide cuánto se movió el precio 5/30/60 s después y su
     tamaño frente al SOL de la curva; los candidatos muestran la mediana.
7. **Alertas** (pestaña propia): el worker arranca un **vigilante** que cada `TRACKER_WATCH_SECONDS` (20 s)
   pide, para cada wallet de la watchlist ya sincronizada, solo las firmas posteriores a la última vista; guarda
   esas transacciones y operaciones sin mover los cursores de la sync (que luego no las descarga otra vez), las
   valora y genera alertas. Tipos: **compra**, **venta** y **convergencia** (`TRACKER_CONVERGENCE_MIN` = 3 grupos
   distintos comprando el mismo token en `TRACKER_CONVERGENCE_MINUTES` = 60; las wallets vinculadas cuentan una
   vez; una alerta nueva por cada nivel alcanzado). Solo alertan operaciones de los últimos
   `TRACKER_ALERT_MAX_AGE_MINUTES` (10), venga de donde venga la operación. Cada alerta guarda hora del bloque,
   de detección y de aviso (retraso medido). Como se usa `finalized`, el retraso mínimo es el de finalización
   (~15 s) más el intervalo del vigilante.
   - **Webhook entrante** para proveedores que empujan transacciones (Helius, QuickNode…):
     `POST /api/hooks/transactions` con cabecera `X-Tracker-Hook-Secret: $TRACKER_HOOK_SECRET`; acepta
     `[{"signature": …}]`, `{"signatures": […]}` u objetos con `transaction.signatures`. Lo no finalizado se
     reintenta. No usa `TRACKER_ACCESS_TOKEN` (tiene su propio secreto).
   - **Webhook saliente** opcional: `TRACKER_ALERT_WEBHOOK_URL` (HTTPS) con `TRACKER_ALERT_WEBHOOK_FORMAT` =
     `json` | `slack` | `discord` | `telegram` (este último con `TRACKER_ALERT_TELEGRAM_CHAT_ID` y la URL
     `https://api.telegram.org/bot<token>/sendMessage`). Tres intentos por alerta. La URL es un secreto: nunca se
     muestra ni se registra.
   - `GET /api/alerts`, `POST /api/alerts/read`; el dashboard muestra el contador, la lista y notificaciones
     del navegador opcionales.
8. **Backtesting de copia**, en dos formas que no se mezclan:
   - **Reconstrucción histórica** («¿Copiarla habría funcionado?» en el perfil; `POST /api/backtest/historical`):
     copia cada operación del líder con varios retrasos (0, 5, 30, 60 y 300 s). La copia se ejecuta al **primer
     precio observado en la base** (operaciones guardadas de cualquier wallet y compras tempranas escaneadas) a partir
     de `t + retraso`, con deslizamiento, impacto opcional y coste por transacción; sin observación en
     `max_staleness_seconds` (300) cuenta como «sin precio». Las ventas se copian en proporción a la del líder; lo
     que queda abierto se valora al último precio observado. Se compara con el ROI del líder en los mismos tokens.
     Retraso 0 es el ideal del precio del líder. Los precios observados son escasos: es una reconstrucción.
   - **Seguimiento prospectivo** (pestaña Alertas; `GET /api/backtest/prospective`): cada alerta de compra o venta
     guarda la cotización real (DEX Screener) al momento y 5 y 60 min después (`alert_quotes`). Se mide la
     rentabilidad de comprar al recibir la alerta (con deslizamiento) a 5 min, a 60 min y al vender el líder. Sin
     información futura; gana valor a medida que se acumulan alertas.
9. **Historial de precios por pool**:
   - **Precios on-chain** (`price_points`): cada compra o venta con contrapartida SOL que aparece en transacciones
     guardadas, de cualquier firmante (el escaneo de semillas ve a muchos traders no seguidos). Verificables.
   - **Velas de pool** (`pool_candles`, GeckoTerminal, público, 30 peticiones/min, ~6 meses gratis): datos de
     terceros, etiquetados. Se piden como rangos (`price_histories`) que el worker descarga hacia atrás con la
     prioridad más baja y retoma donde lo dejó: velas de 1 min para rangos de hasta 3 días y de 1 h para más. Se
     elige el pool más profundo con cotización en SOL. Se encolan solas para cada semilla (primer día en minutos y
     toda su vida en horas), desde el backtest («Descargar velas de sus tokens», `POST /api/backtest/prices`) o a
     mano (`POST /api/prices/{mint}/history?from=&to=`).
   - **Usos**: el backtest usa un precio on-chain si hay uno en el minuto siguiente y, si no, la vela que cubre ese
     momento de forma **pesimista** (compra al máximo, vende al mínimo; `use_candles`); el pico de cada semilla pasa
     a ser el máximo de sus velas (`pool_ohlcv`) cuando supera las otras cotas; y el perfil muestra un **gráfico**
     del token (velas, precios on-chain y las compras ▲ y ventas ▼ de la wallet; `GET /api/prices/{mint}/chart`).
10. Opcional: importa un ledger JSON documentado. Si la cadena ya aporta el evento de la misma firma y mint,
   prevalece el de la cadena.
   La importación es una declaración externa, **no** evidencia verificada por sí misma.
11. **Verificar ledger** comprueba fecha y delta neto del token contra el RPC. No valida
   automáticamente que fuera un swap ni sus precios históricos USD o capitalización.
12. Examina métricas exploratorias, exclusiones y snapshot; añade wallets a la watchlist.

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

**Ranking precalculado.** `GET /api/rankings` ya no analiza todas las wallets en cada consulta: cada wallet
guarda una entrada por conjunto de filtros (`ranking_entries`), con la puntuación, las métricas y el análisis
completo. Toda escritura en sus datos de análisis (operaciones, eventos importados, estado de sync) incrementa
`analysis_revision` de la wallet en la misma transacción, y una entrada solo se usa si su revisión y la versión
del método coinciden; si no, se recalcula antes de responder, así que el resultado es siempre el mismo que
recalculando. Cada sync deja lista la entrada con los filtros por defecto, y el worker recalcula en segundo
plano lo que invalidan el vigilante, las importaciones o una actualización del método (`GET /api/worker` →
`ranking.stale`). `GET /api/wallets/{wallet}/analysis` sin `as_of` usa la misma entrada. Los snapshots
inmutables se guardan solo cuando la API devuelve su id, y las entradas de otros filtros sin recalcular en
7 días se borran.

Variables opcionales:

| Nombre | Uso |
| --- | --- |
| `DATABASE_URL` | Por defecto SQLite; PostgreSQL usa `postgresql+psycopg://…` |
| `SOLANA_RPC_URL` | RPC HTTPS, por defecto `https://api.mainnet-beta.solana.com` |
| (sin variable) | Precios: `https://api.exchange.coinbase.com`, público y sin clave |
| (sin variable) | Cotizaciones actuales: `https://api.dexscreener.com`, público y sin clave |
| (sin variable) | Velas de pool: `https://api.geckoterminal.com`, público y sin clave (30/min) |
| `FOMO_API_KEY` | Bearer para `api.fomoapi.io`, solo servidor |
| `BACKEND_URL` | En Next, por defecto `http://127.0.0.1:8000` |
| `TRACKER_ACCESS_TOKEN` | Opcional: protege la API; Next lo adjunta del lado servidor |
| `CORS_ORIGINS` | Orígenes locales permitidos, separados por comas |
| `TRACKER_AUTO_MIGRATE` | `0`: no migrar al arrancar; solo comprobar que la base está al día |
| `TRACKER_REFRESH_MINUTES` | Worker: intervalo de resincronización de la watchlist (30; 0 desactiva) |
| `TRACKER_EMBEDDED_WORKER` | `1` ejecuta el worker como hilo de la API (solo uso local) |
| `TRACKER_WATCH_SECONDS` | Vigilante de la watchlist: intervalo en segundos (20; 0 desactiva) |
| `TRACKER_ALERT_MAX_AGE_MINUTES` | Antigüedad máxima de una operación para alertar (10) |
| `TRACKER_CONVERGENCE_MIN` / `TRACKER_CONVERGENCE_MINUTES` | Convergencia: grupos distintos (3) y ventana (60) |
| `TRACKER_HOOK_SECRET` | Activa y protege el webhook entrante `/api/hooks/transactions` |
| `TRACKER_ALERT_WEBHOOK_URL` / `_FORMAT` / `TRACKER_ALERT_TELEGRAM_CHAT_ID` | Webhook saliente de alertas |

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
