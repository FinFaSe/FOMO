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
| Posiciones cerradas evaluables | ≥ 30 |
| Ventana de cierres | Últimos 90 días |
| Evidencia | Swaps, USD, fees, market cap histórico e historial completo verificados |

El umbral del win rate conservador es configurable. Wilson se calcula sobre resultados agregados
por mint, para que recomprar el mismo token no infle la muestra independiente.
Con 18 aciertos de 30, el 60% observado **no** pasa el criterio conservador (límite inferior ≈42,3%).
La correlación entre tokens aún puede reducir la independencia. Ver [método](docs/methodology.md).

## Uso

1. Añade una wallet pública en el explorador y pulsa **Sincronizar on-chain**.
2. Inspecciona las firmas finalizadas, errores, deltas y enlaces al explorador de bloques.
   El dashboard limita cada petición a 20 transacciones para respetar el RPC gratuito.
   La API acepta `max_pages` y `page_size`; alcanzar el límite se muestra como cobertura parcial.
3. Importa un ledger JSON documentado mediante el perfil para reconstruir posiciones.
   La importación es una declaración externa, **no** evidencia verificada por sí misma.
4. **Verificar ledger** comprueba fecha y delta neto del token contra el RPC. No valida
   automáticamente que fuera un swap ni sus precios históricos USD o capitalización.
5. Examina métricas exploratorias, exclusiones y snapshot; añade wallets a la watchlist.

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
El ranking puede estar vacío: esta fase todavía no incluye el enriquecimiento histórico y la normalización
completa necesarios para habilitarlo. No significa que no existan traders rentables.
Ver [auditoría y fuentes](docs/audit.md) y [alcance pendiente](docs/roadmap.md).

Variables opcionales:

| Nombre | Uso |
| --- | --- |
| `DATABASE_URL` | Por defecto SQLite; PostgreSQL usa `postgresql+psycopg://…` |
| `SOLANA_RPC_URL` | RPC HTTPS, por defecto `https://api.mainnet-beta.solana.com` |
| `FOMO_API_KEY` | Bearer para `api.fomoapi.io`, solo servidor |
| `BACKEND_URL` | En Next, por defecto `http://127.0.0.1:8000` |
| `TRACKER_ACCESS_TOKEN` | Opcional: protege la API; Next lo adjunta del lado servidor |
| `CORS_ORIGINS` | Orígenes locales permitidos, separados por comas |

La aplicación está diseñada para desarrollo local de un solo investigador. El proxy web no implementa
autenticación de usuarios: no exponerlo públicamente sin añadir login, autorización, TLS y límites por usuario.
Ejecutar FastAPI con un solo worker: el bloqueo de escrituras y el limitador RPC son locales al proceso.

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

`compose.yaml` define PostgreSQL, API y frontend. Elige una contraseña local y mantenla fuera de Git:

```bash
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
docker compose up --build -d
```

Puertos publicados solo en loopback: frontend 3000, API 8000. PostgreSQL usa un volumen persistente.
Para un entorno cloud con proxy/CA, usa también `compose.cloud.yaml`; monta el bundle público de CA
mediante secretos BuildKit y de runtime, sin guardarlo en las capas de imagen. Conserva los proxies
de Docker del entorno. No desactives TLS ni la validación de hashes.

La publicación del entorno cloud y la fusión en GitHub son operaciones independientes.
