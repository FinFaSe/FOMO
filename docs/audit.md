# Auditoría técnica y de fuentes

Revisado el 31 de octubre de 2026. Checkout inicial: `1e9eec9`, únicamente `README.md` (`# FOMO`).
Sin aplicación, manifiestos, tests, servicios, CI o instrucciones AGENTS propias. No había cambios locales.
Herramientas disponibles: Python 3.12, Node 24, uv, npm, Chromium y Docker.

## Fuentes consultadas realmente

| Fuente | Observación | Uso permitido/preparado |
| --- | --- | --- |
| [FOMO Discover](https://fomo.family/discover) | HTTP 200; shell web público, sin dataset utilizable en el HTML | Ninguna ingesta directa; no se investigan endpoints internos ni se usan cookies |
| [Solana getTransaction](https://solana.com/docs/rpc/http/gettransaction) | RPC público responde; `getVersion`, firmas y transacción finalized consultadas | Firmas, slot, tiempos, balances, fees de red, estado; sin USD histórico implícito |
| [Clusters públicos](https://solana.com/docs/references/clusters) | Endpoints compartidos sujetos a límites y retención | Ingesta pequeña con pausas/reintentos; no promesa de archivo completo o SLA |
| [FOMO API docs](https://fomoapi.io/docs) y [OpenAPI](https://fomoapi.io/openapi.json) | API independiente y no oficial, Bearer requerido para datos; documentación pública accesible | Adaptador de resolución de usuario preparado, no autenticado sin clave |
| [FOMO API términos](https://fomoapi.io/terms) | Datos as-is; identidades inferidas; respetar límites; prohíbe redistribución de dataset bruto y mirroring masivo | Uso interno, resolución puntual, sin republicar respuestas brutas |
| [Coinbase Exchange candles](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles) | Público, sin clave; velas de 60 s, máx. ~300 por petición, minutos sin operaciones ausentes | SOL/USD histórico por minuto para valorar operaciones (añadido en noviembre de 2026; no probado desde este entorno por política de red) |
| [GeckoTerminal API](https://apiguide.geckoterminal.com/faq) | Público, 30 llamadas/min; OHLCV por pool en minuto/hora/día con `before_timestamp`; ~6 meses gratis | Velas de pool para picos de semillas y ejecuciones pesimistas del backtest (noviembre de 2026; no probado desde este entorno por política de red) |
| [Pyth Benchmarks](https://docs.pyth.network/benchmarks) | El endpoint público `/v1/shims/tradingview` se retiró el 26/08/2026; la History API nueva requiere clave | No usado |
| [DEX Screener API](https://docs.dexscreener.com/api/reference) | Describe snapshots de pares y perfiles, límites 60/300 solicitudes por minuto según método | Integrado en noviembre de 2026 solo para valorar posiciones abiertas (`/tokens/v1/solana/…`, hasta 30 mints); un precio o market cap actual no demuestra la entrada histórica |

`fomoapi.io` documenta que `/positions` sucede a `/trades`, que el historial de posiciones es incompleto
en todas sus configuraciones y que su PnL reportado no garantiza la separación realized/unrealized/fees.
No se usa su leaderboard como prueba ni se supone que una flag `verified` pruebe dominio de una wallet.
Las condiciones comerciales publicadas pueden cambiar; confirmar plan y licencia antes de ampliar ingesta.
La verificación independiente no se delega en frases de marketing del proveedor.

## Decisión de arquitectura

- Next.js + TypeScript + Tailwind: dashboard y proxy servidor; ninguna clave del proveedor va al navegador.
- FastAPI + Pydantic: contratos estrictos, límites de entrada, RPC y análisis determinista con `Decimal`.
- SQLAlchemy: SQLite para arranque inmediato; PostgreSQL en Compose para persistencia extensible.
- Esquema versionado con Alembic (noviembre de 2026; antes `create_all`): `0001` reproduce el esquema importado,
  `0002` añade las tablas del tracker (tolerante a bases de desarrollo intermedias), `0003` pasa a 64 bits los slots
  y tiempos de `raw_transactions` (en PostgreSQL `Integer` es de 32 bits). Probado en SQLite y en PostgreSQL 16.
- Tablas de wallets, capturas RPC inmutables, observaciones, eventos declarados, snapshots y capturas privadas de fuente.
- Adaptadores separados para Solana y FOMO API; otras redes requieren su propio normalizador, no reutilizar
  contabilidad de balances nativos Solana sin adaptar sus reglas.
- Cola de trabajos en la propia base de datos (tabla `jobs`) y un worker de fondo (`python -m tracker.worker`), sin Redis:
  basta para un investigador y un único worker. Reclamo con UPDATE condicional, rondas encadenadas, reintentos con
  espera exponencial, recuperación por latido y refresco periódico de la watchlist. No es tiempo real: el refresco
  es por intervalo (noviembre de 2026).
- CI ejecuta contabilidad y API con fixtures sintéticos, más types/build del frontend. No usa claves ni escanea wallets.

## Primera fase y límites

La función de extremo a extremo es: añadir wallet → consultar RPC real → conservar evidencia → importar ledger
documentado → reconstruir posiciones → analizar → mostrar exclusiones/snapshots en la web. El verificador solo
comprueba fecha y movimiento neto: un delta aislado no distingue swap, airdrop o transferencia.

No existe todavía un camino de producción que marque swaps, USD, fees y cap histórico como verificados, ni un
normalizador que pruebe cobertura completa. El ranking estricto permanece vacío hasta implementar esos adaptadores.
Esta limitación está expuesta en la web y la API; un usuario no puede importar `verified: true` para eludirla.
La API de FOMO, incluso con clave, no elimina esta limitación por sí sola.
