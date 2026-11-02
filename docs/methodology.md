# Contrato de medición v1

## Contabilidad

Una posición es un ciclo wallet/mint desde balance cero hasta volver a cero. Recomprar tras cerrar abre otro ciclo.
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
Un registro ausente o proveedor incompleto no se trata automáticamente como reorganización probada.
Las transacciones fallidas se almacenan como tales sin convertirlas en trades. Una verificación por delta coincidente
no verifica el intercambio económico ni el precio USD. Importaciones no aceptan campos de verificación del servidor.

Snapshot = SHA-256 de versión de método, wallet, `as_of`, filtros, entradas canónicas y procedencia consumida.
`GET /api/snapshots/{id}` devuelve el resultado inmutable. El mismo ledger, evidencia, filtros y reloj produce el mismo ID.
Cambiar evidencia o parámetros crea otro ID; no se sobrescribe un análisis anterior. No se consumen fills posteriores a `as_of`.
Un snapshot recalculado hoy sobre capturas históricas no equivale a observación prospectiva que existiera entonces.
