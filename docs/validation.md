# Validación ejecutada

Solo se describe lo observado; no se ha probado una estrategia rentable.

## 3 de noviembre de 2026: red real (`python -m tracker.smoke`)

Primera ejecución con acceso a Solana RPC público, Coinbase, DEX Screener, GeckoTerminal y el block engine de
Jito. Tres ejecuciones: una con la wallet firmante de la transacción auditada y dos con una wallet activa en
PumpSwap tomada de la muestra de swaps recientes (2 páginas de 20 en SQLite; 1 de 20 en PostgreSQL 16).
Pasaron todas las comprobaciones obligatorias; las opcionales también cuando hubo tokens operados.

| Comprobación | Resultado observado |
| --- | --- |
| Transacción auditada | Mismo slot (`463752775`) y mismo SHA-256 (`744acb70…`) que en la auditoría del 31 de octubre |
| Normalización de swaps recientes | pump.fun, PumpSwap y Jupiter v6: todas las compras/ventas encontradas, `verified` con contrapartida SOL; ninguna incidencia ni evento inválido |
| Esquema | En `0006_ranking` sobre SQLite y PostgreSQL |
| Ingesta y resincronización | Sin descargar de nuevo lo guardado (observaciones = suma de ambas syncs); 65 operaciones verificadas de PumpSwap en la wallet activa |
| Valoración | Todas las operaciones con USD (Coinbase) y market cap verificado por origen pump.fun |
| Análisis | No elegible (pocas páginas); la wallet activa se marcó como `bot_or_mev_behavior` |
| GeckoTerminal | Pool PumpSwap con cotización SOL; vela de 1 min × SOL/USD = precio de DEX Screener (razón 0,996): `currency=token` devuelve el precio en SOL |
| Jito | `getTipAccounts` devuelve exactamente las 8 cuentas del normalizador |
| RPC público | Limitó la ingesta en una ejecución (`ingesta parcial`, sin pérdida de datos): para uso continuo conviene un RPC de pago (`SOLANA_RPC_URL`) |

No demuestra 90 días de historial ni 30 posiciones ganadoras; las direcciones de traders muestreadas no se
guardan en el repositorio. La suite automática (161 pruebas) pasa en SQLite y en PostgreSQL 16.

## 31 de octubre de 2026: validación inicial

| Comprobación | Resultado |
| --- | --- |
| Contabilidad, estadística y API en base temporal | **41 pruebas pasadas**, fixtures etiquetados como sintéticos |
| Ruff, backend y script RPC real | Correcto |
| TypeScript | Correcto |
| Build Next.js optimizado | Correcto en host y contenedor |
| Builds Docker backend/frontend con CA y TLS verificado | Correctos; dependencias instaladas desde lockfiles |
| Compose: PostgreSQL 16 + FastAPI + Next | Arranque correcto; salud de DB/API comprobada |
| Chromium escritorio/móvil contra stack real | **4 pruebas pasadas**: filtros iniciales, navegación, ausencia de overflow y rechazo de dirección inválida |
| Recorrido adicional en navegador con wallet pública real | Añadir → ingesta acotada → perfil → evidencia → favorita; correcto, sin errores JS |
| RPC desde el contenedor con CA de runtime | Lectura real correcta |
| Deduplicación de evidencia en PostgreSQL | Una sola fila tras almacenar dos veces la misma transacción |
| Reinicio del backend | Salud restablecida, favorita y evidencia conservadas en PostgreSQL |
| `npm start` standalone | Arranque y proxy de salud verificados en puerto alternativo local |
| FOMO API autenticada | **No ejecutada**: no hay clave configurada |
| Historial completo 90 días, 30 posiciones con evidencia de ROI/cap | **No demostrado**; ranking permanece vacío |
| Alertas continuas, convergencia y backtesting | **No implementados** en esta fase |

Las pruebas automatizadas abarcan compras múltiples, costo ponderado, ventas parciales, recompras,
abiertas, transferencias, fees/precios ausentes, ventas sin inventario, pérdidas totales realizadas,
tokens sin precio, ambigüedad temporal, discrepancias de evidencia, conflictos finalized, duplicados,
ventana sin información futura, Wilson, recompras del mismo mint, concentración de ganancias,
snapshots reproducibles y concurrentes, importación atómica y límites de confianza.
También se comprueba que cierres con costos desconocidos no se oculten al evaluar un historial que
de otro modo tendría 30 posiciones ganadoras. Un futuro detector de entidades relacionadas o un
motor de backtesting necesitará sus propias pruebas; los tests actuales no validan esos módulos pendientes.

El runner reporta un aviso de deprecación de Starlette sobre TestClient/httpx; las pruebas ejecutaron
y finalizaron. No se deshabilitaron assertions, TLS ni hashes para obtener resultados.

## Evidencia pública consultada

Transacción usada en la auditoría y prueba RPC:

- [Transacción Solana](https://solscan.io/tx/k5QYVE2czvAA5bP8wjPF5rdgxEsCzzN4XBQaLJW5VkGTxsbv5yedb5hpodSRBjmRfhPDvAGJeiNM9mYrcCyX2oh).
- Slot: `463752775`, finalized; transacción versión 1.
- SHA-256 de JSON canónico del resultado RPC:
  `744acb70be90f0981aac9354171f92324f67bc8f9e75e07dbf698e7ad68b7425`.
- Firmante observado: `Bvg6QYGoRFihvntWNwhgcK9vYq7jPNwekBDVU97f9tU3`.
  **No se ha probado que sea usuario de FOMO ni un trader rentable.**

En el recorrido del navegador se ingirió además la transacción pública
`2ypgegBS87cXUsFAA46FQ2e5s84oQbxSssWNvopquWMUsUKUXUpAGPREhHQUHPTw4GHZaRZS4kzuY8Jv4JM6CXVu`,
con digest `0e5ef2bd109088486c0950fbb3215ebdf383dbe4c7339e9ea4bb149ad8730938`.
No se dedujo una compra o ganancia de ese movimiento. Perfil: muestra evaluable 0, score verificado null,
clasificación no elegible. Esto valida la ingesta y las barreras contra afirmaciones infundadas.

Los artefactos de esta máquina están en `.data/live-smoke.json`, `.data/browser-live-smoke.json`,
`.data/dashboard-desktop.png` y `.data/dashboard-mobile.png` (ignorados en Git). Son evidencia de esta
ejecución, no garantía de disponibilidad futura del RPC público. La base de datos contiene un candidato
público y capturas reales; nunca se cargaron posiciones sintéticas para rellenar el dashboard.

Estos resultados describen la validación inicial. La publicación del código y del entorno se gestiona
por separado. Los contenedores son procesos locales; los comandos de arranque y las limitaciones
están documentados en README.
