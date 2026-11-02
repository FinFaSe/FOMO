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
| [DEX Screener API](https://docs.dexscreener.com/api/reference) | Describe snapshots de pares y perfiles, límites 60/300 solicitudes por minuto según método | Investigado, no integrado; un precio o market cap actual no demuestra la entrada histórica |

`fomoapi.io` documenta que `/positions` sucede a `/trades`, que el historial de posiciones es incompleto
en todas sus configuraciones y que su PnL reportado no garantiza la separación realized/unrealized/fees.
No se usa su leaderboard como prueba ni se supone que una flag `verified` pruebe dominio de una wallet.
Las condiciones comerciales publicadas pueden cambiar; confirmar plan y licencia antes de ampliar ingesta.
La verificación independiente no se delega en frases de marketing del proveedor.

## Decisión de arquitectura

- Next.js + TypeScript + Tailwind: dashboard y proxy servidor; ninguna clave del proveedor va al navegador.
- FastAPI + Pydantic: contratos estrictos, límites de entrada, RPC y análisis determinista con `Decimal`.
- SQLAlchemy: SQLite para arranque inmediato; PostgreSQL en Compose para persistencia extensible.
- Tablas de wallets, capturas RPC inmutables, observaciones, eventos declarados, snapshots y capturas privadas de fuente.
- Adaptadores separados para Solana y FOMO API; otras redes requieren su propio normalizador, no reutilizar
  contabilidad de balances nativos Solana sin adaptar sus reglas.
- No Redis ni worker permanente aún: tareas acotadas síncronas; se informa cobertura parcial, no se finge tiempo real.
- CI ejecuta contabilidad y API con fixtures sintéticos, más types/build del frontend. No usa claves ni escanea wallets.

## Primera fase y límites

La función de extremo a extremo es: añadir wallet → consultar RPC real → conservar evidencia → importar ledger
documentado → reconstruir posiciones → analizar → mostrar exclusiones/snapshots en la web. El verificador solo
comprueba fecha y movimiento neto: un delta aislado no distingue swap, airdrop o transferencia.

No existe todavía un camino de producción que marque swaps, USD, fees y cap histórico como verificados, ni un
normalizador que pruebe cobertura completa. El ranking estricto permanece vacío hasta implementar esos adaptadores.
Esta limitación está expuesta en la web y la API; un usuario no puede importar `verified: true` para eludirla.
La API de FOMO, incluso con clave, no elimina esta limitación por sí sola.
