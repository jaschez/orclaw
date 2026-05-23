# Batch Planner — descriptor (no LLM, lo dejo aquí por consistencia)

> El batch planner NO usa Claude. Es algoritmo puro. Este archivo documenta sus inputs/outputs y comportamiento de cara al sistema completo.

## Función

Lee el estado de issues + PRs en GitHub y calcula los batches actuales que el orchestrator debe ejecutar.

## Inputs

- Todas las issues open en `${TARGET_REPO}`
- Todos los PRs open contra `develop`
- Estado SQLite previo (`batches` table)

## Outputs

- Actualización de `batches` table en SQLite con la nueva layered partition
- Métricas para `/status/planner` endpoint
- Señal al orchestrator si el batch actual ha cambiado

## Reglas de exclusión (qué NO entra en ningún batch)

1. Issue con label `ops` (responsabilidad del CEO/CTO, manual)
2. Issue con label `do-not-implement` (kill switch por-issue)
3. Issue con label `wontfix`
4. Issue cerrada
5. Issue con PR abierto referenciándola (vía GraphQL closingIssuesReferences)
6. Issue marcada como `agent:start` Y con run de implementer activo en SQLite
7. Issue cuya última implementación falló ≥ 3 veces consecutivas

## Algoritmo

Ver `docs/batch-algorithm.md` para el detalle. TL;DR:

1. Build grafo dirigido issue→dependentes
2. Topological layering (Kahn modificado)
3. Layer 0 = no deps abiertas → primer batch
4. Layer K = deps en layers < K
5. Si current_layer está totalmente en `in_progress` o `merged`, calcular next executable layer

## Triggers

- Timer systemd cada 10 min
- Webhook GitHub `issues.closed`, `pull_request.merged`, `pull_request.opened`
- Llamada explícita `orclaw planner run`
- Tras un `orchestrator.batch_completed` event

## Persistencia

```sql
-- ya documentado en docs/batch-algorithm.md
batches (id, layer, issue_number, status, implementer_run_id, pr_number, created_at, updated_at)
```

## Logs

Cada run del planner registra:

```json
{
  "ts": "2026-05-22T18:30:00Z",
  "duration_ms": 850,
  "issues_scanned": 12,
  "layers_computed": 3,
  "current_batch_issues": [201, 202, 207],
  "blocked_issues": [203, 204, 205],
  "excluded_issues": {
    "201_pr_open": true,
    "208_ops": true
  },
  "cycle_detected": false
}
```

A journald via `logger -t orclaw-batch-planner`.

## Cuándo el planner debe alertar

- **Dependency cycle detected**: abre issue `engine:dep-cycle-detected` con los IDs del ciclo
- **Issue huérfana** (no parseable, label inconsistente): warning a journald + flag en SQLite
- **More than N layers** (>10): warning ("plan demasiado profundo, considerar paralelización del producto")
- **Batch vacío 5+ runs seguidos**: notifica que todo está bloqueado por OPS pendientes
