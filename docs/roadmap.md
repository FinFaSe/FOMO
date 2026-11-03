# Siguientes fases

Implementado: primera ruta funcional de investigación, no la totalidad de la especificación.

1. **Normalización verificable** (parcial: swaps por cambio de balance con venues reconocidos, en SOL/USDC/USDT;
   ver método): decodificar swaps por programa y versión, cantidades/fees, rutas multipool,
   transacciones intrabloque ordenadas y transferencias de inventario relacionadas. Verificar cobertura desde la
   apertura de cada lote con un RPC de archivo; reconciliar saldos y no ocultar pérdidas fuera de la muestra.
2. **Enriquecimiento histórico** (parcial: SOL/USD por minuto, market cap verificado para mints de pump.fun
   —también graduados— y estimado para mints sin autoridad de emisión; posiciones abiertas a precio actual):
   precios USD con timestamp y fuente autorizada, supply circulante histórico
   para cap (no FDV), liquidez ejecutable y fees de red valorados al momento. Definir adaptadores que produzcan
   hechos verificables del lado servidor, sin importar flags de confianza de clientes.
3. **Discovery ampliado** (parcial: semillas con escaneo de la bonding curve desde la creación, compradores
   tempranos con market cap de entrada y marcas, cruce entre semillas con aciertos por múltiplo; falta un pico
   histórico verificado vía OHLC y semillas automáticas): semillas históricas que incluyan tokens fallidos y desaparecidos; índices de swaps,
   coincidencia entre tokens y wallets; controlar sesgos y separar candidatos de resultados validados.
4. **Rankings especializados y comparación**: Early Hunters, Consistent Winners, ≥1.000% (11×), High Accuracy,
   Rising y Replicable. Añadir señales de entidades/deployers, límites de confianza y pesos configurables.
5. **Monitoreo y alertas** (parcial: worker persistente, vigilante de la watchlist cada 20 s, webhook entrante,
   alertas de compra/venta/convergencia con retraso medido y entrega por webhook saliente; falta WebSocket y
   `confirmed` para bajar el retraso): worker persistente, colas/Redis cuando se justifique, eventos finalizados,
   deduplicación de notificaciones, timestamps de detección/envío y latencia medida. No WebSocket perpetuo en Actions.
6. **Convergencia y riesgo** (parcial: señales de bot/MEV, origen de fondos, entidades por financiación y compras
   juntas, marcas de insider y coste de copia en descubrimiento): agrupar wallets vinculadas para evitar señales independientes falsas;
   fuentes verificables para concentración, manipulaciones aparentes y restricciones de venta.
7. **Backtesting**: registrar primero observaciones prospectivas y cotizaciones realmente disponibles tras alertas;
   simular slippage/fees/impacto/capacidad/fallos con reloj de disponibilidad. Separar reconstrucción histórica de
   observaciones prospectivas; no usar precios futuros para decidir o simular fills.

La clave FOMO API solo desbloquea su adaptador de identidad actual. Antes de ampliar su uso: confirmar licencia,
costos, autorización y cobertura con el proveedor, y medir independientemente cuánto historial puede recuperarse.
