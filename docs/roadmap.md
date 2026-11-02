# Siguientes fases

Implementado: primera ruta funcional de investigación, no la totalidad de la especificación.

1. **Normalización verificable**: decodificar swaps por programa y versión, cantidades/fees, rutas multipool,
   transacciones intrabloque ordenadas y transferencias de inventario relacionadas. Verificar cobertura desde la
   apertura de cada lote con un RPC de archivo; reconciliar saldos y no ocultar pérdidas fuera de la muestra.
2. **Enriquecimiento histórico**: precios USD con timestamp y fuente autorizada, supply circulante histórico
   para cap (no FDV), liquidez ejecutable y fees de red valorados al momento. Definir adaptadores que produzcan
   hechos verificables del lado servidor, sin importar flags de confianza de clientes.
3. **Discovery ampliado**: semillas históricas que incluyan tokens fallidos y desaparecidos; índices de swaps,
   coincidencia entre tokens y wallets; controlar sesgos y separar candidatos de resultados validados.
4. **Rankings especializados y comparación**: Early Hunters, Consistent Winners, ≥1.000% (11×), High Accuracy,
   Rising y Replicable. Añadir señales de entidades/deployers, límites de confianza y pesos configurables.
5. **Monitoreo y alertas**: worker persistente, colas/Redis cuando se justifique, eventos finalizados,
   deduplicación de notificaciones, timestamps de detección/envío y latencia medida. No WebSocket perpetuo en Actions.
6. **Convergencia y riesgo**: agrupar wallets vinculadas para evitar señales independientes falsas;
   fuentes verificables para concentración, manipulaciones aparentes y restricciones de venta.
7. **Backtesting**: registrar primero observaciones prospectivas y cotizaciones realmente disponibles tras alertas;
   simular slippage/fees/impacto/capacidad/fallos con reloj de disponibilidad. Separar reconstrucción histórica de
   observaciones prospectivas; no usar precios futuros para decidir o simular fills.

La clave FOMO API solo desbloquea su adaptador de identidad actual. Antes de ampliar su uso: confirmar licencia,
costos, autorización y cobertura con el proveedor, y medir independientemente cuánto historial puede recuperarse.
