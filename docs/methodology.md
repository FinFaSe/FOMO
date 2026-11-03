# Contrato de medición v1

## Contabilidad

Una posición es un ciclo wallet/mint desde balance cero hasta volver a cero, o a **polvo**: si tras una venta o salida
queda como máximo el 0,01% de la mayor cantidad tenida en el ciclo, el ciclo se cierra y ese resto se da de baja a
valor cero (su costo remanente se suma al costo asignado, así que el PnL no mejora). Vender o mover después ese polvo
no abre un ciclo ni cuenta como inventario faltante, y su importe se ignora; si una venta de un ciclo nuevo incluye
el polvo anterior, solo la parte proporcional del cobro pertenece al ciclo nuevo. Recomprar tras cerrar abre otro ciclo.
Se reconstruye **todo** el ledger disponible anterior al snapshot, incluso compras anteriores a la ventana, para no
perder el costo inicial. Se incluyen en la métrica de 90 días los ciclos cuyo cierre cae en `[as_of − 90 días, as_of]`.
Posiciones abiertas se muestran aparte; su PnL parcial realizado no cuenta como un win ni se mezcla en el ranking.

- Compra: `costo_remanente += gross_usd + fees_usd`, `cantidad += tokens`.
- Venta: asignar `costo_remanente × cantidad_vendida / cantidad_previa`; restarlo del remanente.
- Recuperado neto: `gross_usd_venta − fees_usd_venta`.
- PnL realizado: recuperado neto menos costo asignado a lo vendido.
- ROI realizado: `100 × PnL_realizado / costo_asignado`. +500% es 6×, +1.000% es 11×.
- Costo cero, fees o USD desconocidos, inventario faltante, movimientos incompatibles o transferencias contaminan
  la evaluabilidad. Nunca se inventa un costo cero para una entrada transferida.

Se exige conocer la capitalización circulante de **cada compra** del ciclo, a su fecha, bajo el máximo; FDV es un
campo diferente. Ni supply actual, precio actual, ATH, FDV ni crecimiento del market cap sustituyen esos datos.
La salida no requiere cap para calcular ROI si efectivo y costos sí están documentados.
El ledger conserva declaraciones de FDV/liquidez sin utilizarlas para inferir USD o cap faltantes.
Precios y cantidades se manejan con `Decimal`; importes en JSON se serializan como strings.

Si varias firmas para el mismo mint tienen exactamente el mismo segundo y no se dispone de orden intrabloque,
la posición se marca ambigua. Una ordenación lexicográfica determinista no se considera prueba del orden económico.
El MVP admite movimientos finales de token; no pretende normalizar token rebasing, impuestos Token-2022, MEV,
bundles, migraciones, perps, NFT o rutas complejas sin un adaptador específico.

## Normalización on-chain (`balance-delta-v1`)

Cada transacción guardada se convierte en eventos por mint a partir de los cambios netos de la wallet, no de
decodificar instrucciones, de modo que cualquier ruta por un venue reconocido se normaliza igual.

- Tramo SOL = Δlamports de la wallet + Δlamports de cada cuenta de token cuyo `owner` es la wallet + fee de red si
  la wallet la pagó (`accountKeys[0]`). El rent de sus propias cuentas creadas o cerradas se compensa; el wSOL se
  cuenta por los lamports de su cuenta y no por su delta de token, para no contarlo dos veces.
- Un solo token contra una sola contrapartida de signo opuesto (SOL, USDC o USDT) es compra o venta; sin
  contrapartida es transferencia. Token por token o varias contrapartidas conservan el inventario pero no el
  precio (`multi_leg_swap_quote_not_allocatable`). Las transacciones fallidas no generan eventos.
- `verified` exige programa reconocido + firma de la wallet + contrapartida identificada; si no, `inferred`.
  Verificar la operación no verifica USD ni market cap: siguen `unknown`.
- La fee de red se imputa al primer evento de la transacción. Las fees del venue ya están dentro del importe
  intercambiado (precio efectivo). Propinas (Jito) y rent pagado a cuentas ajenas quedan dentro del costo.
- Las posiciones en SOL reutilizan el costo promedio ponderado; su ROI difiere del ROI en USD si SOL se movió.
  El win rate de esa vista es por posición, sin agrupar por token: es exploratorio.

## Valoración USD y market cap de entrada

- SOL/USD = cierre de la vela de 1 minuto de Coinbase Exchange (`SOL-USD`) que contiene el `blockTime`; si ese
  minuto no tuvo operaciones, la vela más cercana a ≤5 minutos (se registra el desfase). Se guardan `low`/`high`
  como banda de incertidumbre. Sin vela cercana la operación queda `unpriced` y su posición no es evaluable.
- `gross_usd` = contrapartida × SOL/USD (o la cantidad de USDC/USDT, a la par); `fees_usd` = fee de red × SOL/USD.
- Market cap = precio USD por token × límite superior de suministro. Solo se marca `verified` para mints con una
  operación guardada contra la bonding curve de pump.fun (prueba de origen) y 6 decimales: se crean con
  1.000.000.000 tokens y sin autoridad de emisión, y las quemas solo reducen el suministro, así que el market cap
  nunca queda subestimado frente al filtro de market cap máximo. El resto de mints queda `unknown`.
- Prueba de origen pump.fun: una operación guardada contra la bonding curve, o una cuenta propiedad del programa
  de pump.fun en la PDA `["bonding-curve", mint]` (derivada con `solders`, leída con `getMultipleAccounts`).
- Mints sin esa prueba y sin autoridad de emisión: market cap `estimated` = precio × suministro actual. Sin
  autoridad nadie puede emitir, pero las quemas posteriores a la entrada reducen el suministro actual, así que la
  estimación puede quedar por debajo del real: se muestra y se usa en métricas exploratorias, nunca en el ranking.
  Con autoridad de emisión activa: `unknown`.
- Pyth Benchmarks no se usa: su endpoint público TradingView se retiró el 26 de agosto de 2026.

## Orden dentro del mismo segundo

`blockTime` tiene resolución de un segundo. Los fills de la cadena llevan su slot: transacciones del mismo mint en
el mismo segundo pero en slots distintos se ordenan por slot y no son ambiguas. En el mismo slot el orden
intrabloque es desconocido (no se consulta `getBlock`) y la posición se marca `ambiguous_transaction_order`, igual
que los empates sin slot (ledger importado).

## Posiciones abiertas

PnL no realizado = cantidad × precio actual − costo remanente (costo promedio ponderado). Precio del par con más
liquidez USD en DEX Screener donde el mint es el token base; caché de 60 s. Es una marca de mercado, no un precio de
ejecución, y no forma parte de snapshots ni del ranking.

## Historial completo

`history_complete` es verdadero solo si el escaneo incremental cubre `[as_of − días, as_of]`: sin catch-up
pendiente, `covered_from ≤ inicio de ventana`, `caught_up_at ≥ as_of`, sin `blockTime` ausentes y con todas las
transacciones normalizadas por la versión vigente. `caught_up_at` es el inicio del escaneo menos 120 s, por el
retraso de `finalized`. Sin `as_of` explícito, el análisis y el ranking usan el `caught_up_at` de cada wallet.

## Muestra y consistencia

El win rate observado es `ciclos con PnL > 0 / ciclos cerrados evaluables`; break-even permanece en el denominador.
Para el intervalo de Wilson 95% se agrega el PnL de los ciclos evaluables por **mint** en la ventana y se considera
acierto del grupo si el total es positivo. El número de mints es la muestra efectiva usada por el intervalo.
No se presenta este intervalo como una garantía: tokens distintos pueden compartir factores/entidades y no ser independientes.
El mínimo de 30 corresponde a ciclos evaluables; un número pequeño de mints se refleja en el intervalo y el score.

El ranking exige el 60% observado **y**, por defecto, límite inferior Wilson ≥60%, además de al menos un gran
ganador realizado temprano, historial completo y todas las posiciones evaluables con swaps, valoraciones y cap verificados.
El análisis de una importación puede mostrar cifras provisionales; nunca crea alta confianza con una URL y una firma.

## Score exploratorio

Pesos iniciales: 25 consistencia, 20 frecuencia ≥umbral de gran ganador, 15 frecuencia ≥1.000%, 15 retorno/riesgo,
10 entradas tempranas, 10 replicabilidad y 5 calidad. Los filtros son configurables; pesos y método quedan versionados
en esta fase (no se ofrece aún un editor de ponderaciones).

- Ajuste de muestra `n_tokens / (n_tokens + 30)` para consistencia y retorno/riesgo.
- Consistencia = semanas UTC con PnL positivo / todas las semanas UTC tocadas por la ventana, incluidas inactivas.
- Frecuencias temprana y de grandes ganadores usan límite inferior Wilson por mint.
- Retorno/riesgo = ROI agregado positivo acotado a 1, reducido por muestra y drawdown de PnL realizado/costo asignado.
- Replicabilidad = **0** mientras no exista backtest neto después de alerta; riesgo de copia **desconocido**.
- Calidad = proporción de posiciones con prueba completa multiplicada por cobertura completa del historial.
- Concentración: se resta al score una fracción `max(0, cuota_del_mayor_beneficio − 0,5)`, hasta 50%.

Una curva de PnL realizado no es una curva de patrimonio: el drawdown resultante no mide pérdidas flotantes.
Wash trading, vínculo con deployers y liquidez insuficiente aún no se detectan automáticamente. No se afirma ausencia
de esas condiciones, fraude o replicabilidad sin evidencia. El score exploratorio no es recomendación de inversión.

## Procedencia y snapshots

Cada RPC conserva respuesta original, origen sin claves, slot, timestamp original, fecha de captura y SHA-256.
Duplicados no cambian evidencia; una respuesta finalized distinta genera conflicto, no sobrescritura.
La sincronización incremental reutiliza la evidencia almacenada sin volver a pedirla; la comparación con
una nueva descarga ocurre al **Verificar ledger** y en el modo `revalidate`. La cobertura de firmas se
registra por wallet (`sync_state`): todo lo listado entre `head` y `tail` está almacenado, y
`covered_from` indica desde qué `blockTime` la ventana está completa. Esto describe firmas, no swaps
normalizados: `normalized_history_complete` sigue siendo falso.
Un registro ausente o proveedor incompleto no se trata automáticamente como reorganización probada.
Las transacciones fallidas se almacenan como tales sin convertirlas en trades. Una verificación por delta coincidente
no verifica el intercambio económico ni el precio USD. Importaciones no aceptan campos de verificación del servidor.

Snapshot = SHA-256 de versión de método, wallet, `as_of`, filtros, entradas canónicas y procedencia consumida.
`GET /api/snapshots/{id}` devuelve el resultado inmutable. El mismo ledger, evidencia, filtros y reloj produce el mismo ID.
Cambiar evidencia o parámetros crea otro ID; no se sobrescribe un análisis anterior. No se consumen fills posteriores a `as_of`.
Un snapshot recalculado hoy sobre capturas históricas no equivale a observación prospectiva que existiera entonces.
