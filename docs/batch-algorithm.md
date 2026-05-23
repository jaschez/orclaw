# Batch algorithm — cómo se forman los lotes paralelos

## Objetivo

Dado un conjunto de issues con dependencias declaradas en sus bodies (`Blocked by #N`), producir una secuencia de **batches**:

- **Dentro** de un batch: issues completamente independientes entre sí → paralelas
- **Entre** batches: secuenciales (batch K+1 espera a que TODOS los issues de batch K estén mergeados)

Esto es exactamente lo que pediste: lotes paralelos sin interdependencia, secuenciales entre lotes.

## Definiciones

- **Issue abierta no-OPS**: tiene state=OPEN y NO tiene label `ops`
- **Dep abierta**: una "Blocked by #N" donde la issue #N está en state=OPEN
- **Layer (Kahn)**: la profundidad mínima en el grafo de dependencias

## Algoritmo

```python
def compute_batches(issues: list[Issue]) -> list[list[Issue]]:
    """
    Returns layers of issues, where each layer can be processed in parallel.
    Layer 0 has no dependencies. Layer K depends only on layers < K.
    """
    # 1. Build adjacency: blocker → blocked
    blocks = defaultdict(set)
    blocked_by = defaultdict(set)
    candidates = {i.number: i for i in issues if not i.is_ops()}

    for issue in candidates.values():
        for dep_num in parse_blocked_by(issue.body):
            # Only count deps that are still OPEN — closed ones don't block
            if dep_num in candidates:
                blocks[dep_num].add(issue.number)
                blocked_by[issue.number].add(dep_num)

    # 2. Kahn's topological layering
    layers = []
    in_degree = {n: len(blocked_by[n]) for n in candidates}
    remaining = set(candidates.keys())

    while remaining:
        # Current layer = all issues with zero remaining deps
        current_layer = sorted(
            [n for n in remaining if in_degree[n] == 0],
            key=lambda n: (priority_rank(candidates[n]), n),
        )

        if not current_layer:
            # Cycle in dependencies — should not happen. Surface as error.
            raise DependencyCycle(remaining=remaining, in_degree=in_degree)

        layers.append([candidates[n] for n in current_layer])
        for n in current_layer:
            remaining.discard(n)
            # Decrement in_degree of issues that this one blocked
            for unblocked in blocks[n]:
                in_degree[unblocked] -= 1

    return layers


def priority_rank(issue: Issue) -> int:
    """P0 first, P1 second, anything else last."""
    labels = {l.name for l in issue.labels}
    if "P0" in labels: return 0
    if "P1" in labels: return 1
    return 2


def parse_blocked_by(body: str) -> set[int]:
    """Extract Ns from 'Blocked by #N' patterns. Case-insensitive."""
    return {int(m) for m in re.findall(r"[Bb]locked by #(\d+)", body or "")}
```

## Cómo se ejecuta un batch

El Orchestrator coge el primer layer no-vacío y NO bloqueado por implementaciones activas:

```python
def next_executable_batch(layers: list[list[Issue]], state: OrchestratorState) -> list[Issue]:
    """
    Pick the first layer where NO issue has an open PR or active implementer.
    Apply concurrency cap based on budget.
    """
    for layer in layers:
        issues_in_progress = state.issues_with_open_pr() | state.issues_with_active_implementer()
        if not any(i.number in issues_in_progress for i in layer):
            # This layer is ready to start
            cap = budget.current_concurrency_cap()  # 2..5 según quota restante
            return layer[:cap]
    return []  # All layers fully in progress, just wait
```

## Filtros adicionales aplicados al batch elegido

Antes de spawnar implementers, el Orchestrator filtra cada issue del batch:

1. **`ops` label** → excluir (OPS son tuyos, no del agente)
2. **`do-not-implement` label** → excluir (kill switch por-issue)
3. **PR abierto referenciando la issue (closingIssuesReferences)** → excluir
4. **`agent:start` label ya presente** → excluir (algún proceso previo la cogió)
5. **Implementación previa fallida ≥ 3 veces** → excluir + alertar al CEO/CTO

## Ejemplo trabajado con datos reales

Estado a 22 may después del cleanup. Issues abiertas no-OPS no-cerradas:

```
#110 [P1] Sentry integration: no deps (todas las "Blocked by" están cerradas)
#131 [META] Daily tracker: META, no se procesa
```

Como tras la limpieza de hoy quedan pocas, el batch actual es:

```
Layer 0 = [#110]
Layer 1+ = vacío
```

Batch a ejecutar: solo `#110`. El Orchestrator spawna 1 implementer, espera, y cuando termine el array se vacía → engine idle hasta que el Specialist genere issues nuevas.

## Ejemplo más interesante (V1 al principio)

Si hubiéramos arrancado la engine al principio del plan V1 con las 29 issues originales:

```
Layer 0:  [#88 schema, #107 legal pages, #108 cookie banner, #116 test infra]
          (4 paralelas, ninguna depende de nada abierto)

Layer 1:  [#89, #91, #95, #98, #109, #112]
          (todas dependen de #88 y/o #107 que están en layer 0)

Layer 2:  [#90, #92, #94, #96, #97, #99, #100, #111, #113, #114]

Layer 3:  [#93, #101, #102, #103, #104, #106]

Layer 4:  [#105, #115]

Layer 5+: ya cerradas o vacías
```

Con concurrency cap de 5 (presupuesto generoso), cada layer toma ≈ tiempo del implementer más lento (~20 min) más review. ~30-40 min por layer. 5 layers = 2-3 h reales para todo V1, si Claude no falla y no hay conflictos.

Con cap de 2 (presupuesto justo), ~5-6 h reales.

## Triggers del Batch Planner

El planner re-corre el algoritmo cuando:

| Evento | Latencia |
|---|---|
| Cron systemd timer (`orclaw-batch-planner.timer`) | Cada 10 min |
| Webhook GH `issues.closed` o `pull_request.closed.merged` | < 30 s |
| Manual: `orclaw batch-planner run` | inmediato |
| Tras un `claim_batch` del Orchestrator | inmediato |

## Persistencia del estado del planner

SQLite `engine.db`:

```sql
CREATE TABLE batches (
  id INTEGER PRIMARY KEY,
  layer INTEGER NOT NULL,
  issue_number INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'in_progress', 'merged', 'failed', 'skipped')),
  implementer_run_id TEXT,
  pr_number INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_batches_layer_status ON batches(layer, status);
CREATE UNIQUE INDEX idx_batches_issue ON batches(issue_number) WHERE status != 'failed';
```

`status` lifecycle:
- `pending` — calculado por planner, esperando que el orchestrator lo coja
- `in_progress` — orchestrator spawneó el implementer
- `merged` — PR se mergeó, issue cerrada
- `failed` — implementer falló 3 veces, requiere revisión humana
- `skipped` — la issue fue cerrada externamente sin nuestro PR

El orchestrator solo avanza al layer K+1 cuando TODAS las issues del layer K están en `merged | skipped | failed`. (`failed` no bloquea — se reporta y se sigue.)

## Edge cases manejados

- **Issue cambia de deps mientras está en cola**: el planner recalcula en cada run. Si las deps de una issue ya en `pending` cambian, se reasigna a otro layer.
- **Issue se cierra a mano (humano) mientras está `in_progress`**: el orchestrator cancela el implementer en su próximo health check, marca status `skipped`.
- **Dep cycle accidental** (A blocked by B, B blocked by A): el planner detecta y bloquea todo el ciclo, abre una issue interna `engine:dep-cycle-detected` con los IDs implicados.
- **Issue OPS marcada por error como agent:ready**: el planner SIEMPRE excluye `ops`, label superior. Si quieres que un OPS sea automatizable, primero le quitas el label `ops`.

## Métricas del planner

Cada run del planner emite a `engine.db.metrics`:

- Número de layers totales calculados
- Número de issues en current_batch
- Tiempo de cálculo (debería ser sub-segundo para <500 issues)
- Issues "huérfanas" detectadas (sin labels esperados, sin deps parseables)

Visible en el dashboard en `/status/planner`.
