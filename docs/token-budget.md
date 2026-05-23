# Cuota Pro plan — estrategia de observación y control

> **Decisión de auth**: Cero API key de Anthropic. Todo Claude vía `@claude` en GitHub Actions consumiendo el plan Pro vía OAuth. Ver `pro-plan-strategy.md`.

## Realidad de la cuota

- Plan Pro: ~225 mensajes / 5 h rolling window (no documentado público exacto; usamos esta estimación)
- Compartido entre: uso interactivo del CEO/CTO + todas las acciones de la engine (specialist, implementer, reviewer)
- Sin API para consultar % restante en tiempo real
- Cuando se satura, Claude devuelve 429 / auth error → claude.yml falla con conclusion `failure`

## Lo que NO tracking

- ❌ Tokens input/output (Anthropic no lo expone para Pro)
- ❌ Coste en USD/EUR (no hay factura, no aplica)
- ❌ Cache hit ratio (no medible desde nuestro lado)

## Lo que SÍ medimos (proxies)

- ✅ **Número de workflow runs de claude.yml** por hora / día / 5h window
- ✅ **Duración de cada run** (latencia alta = backend saturado)
- ✅ **Conclusion de cada run** (success / failure / cancelled / skipped)
- ✅ **Frecuencia de auth errors** (proxy directo de saturación)
- ✅ **PRs abiertos por minuto** (output efectivo del sistema)

## Tabla `runs` en SQLite

```sql
-- ya definida en orchestrator/state/schema.sql
runs (
  id TEXT PRIMARY KEY,
  agent TEXT,            -- 'specialist' | 'implementer' | 'reviewer'
  issue_number INTEGER,
  pr_number INTEGER,
  status TEXT,           -- 'queued' | 'running' | 'success' | 'failed' | 'rate_limited' | 'timeout'
  started_at TEXT,
  finished_at TEXT,
  duration_seconds INTEGER,
  workflow_run_id INTEGER,  -- GH Actions run ID para correlate
  notes TEXT
)
```

Cada `@claude` mention que la engine postea queda registrada al postear (status `queued`), se actualiza tras observar el workflow run correspondiente.

## Concurrencia

```toml
# config/concurrency.toml
[concurrency]
max_in_flight = 2          # nunca más de 2 @claude mentions simultáneas vivas
default_in_flight = 1      # sequential por defecto, burst a 2 solo si quota healthy

[backoff]
# Si detectamos 2 failures con auth error en últimos 10 min → asumimos saturación
saturation_threshold_failures = 2
saturation_window_minutes = 10
saturation_cooldown_minutes = 30        # tras detectar saturación, esperar este tiempo

# Si una acción individual falla, retry exponencial
retry_initial_seconds = 60
retry_max_attempts = 3
retry_max_total_minutes = 30
```

## Cómo el orchestrator regula el ritmo

Pseudocódigo:

```python
async def orchestrator_loop():
    while running:
        if is_saturated():
            await sleep(saturation_cooldown_minutes * 60)
            continue

        active = active_in_flight_count()
        if active >= max_in_flight:
            await sleep(30)
            continue

        batch = next_executable_batch()
        if not batch:
            await sleep(60)  # nothing to do
            continue

        # Post @claude mention for next issue
        issue = batch.next()
        await post_comment(issue, build_implementer_prompt(issue))
        record_run(issue, status='queued')

        # Wait a bit before posting next, even if cap allows it
        # (gives claude.yml time to start without flooding GH webhooks)
        await sleep(30)


def is_saturated() -> bool:
    failures = db.recent_failures(window_minutes=saturation_window_minutes)
    return len(failures) >= saturation_threshold_failures


def active_in_flight_count() -> int:
    # Issues with agent:start label AND no merged PR yet
    return db.count_active_runs(status_in=['queued', 'running'])
```

## Dashboard `/status/quota`

Lo que muestra:

```
┌─────────────────────────────────────────────────────────┐
│ Quota observation (Pro plan)                            │
│                                                         │
│ Last 5h window:                                         │
│   Total @claude mentions posted:    23                  │
│   Successful runs:                    20                │
│   Failed runs:                         1                │
│   Rate-limited:                        2                │
│   Avg run duration:               4m 12s                │
│                                                         │
│ Saturation status: 🟢 healthy                           │
│ (heuristic: 0 failures in last 10 min)                  │
│                                                         │
│ Currently in flight: 1                                  │
│ - #142 cookie banner (implementer, queued 0m32s ago)    │
│                                                         │
│ Last 24h:                                               │
│   88 mentions · 76 success · 8 failed · 4 rate_limited  │
│                                                         │
│ Last 7d:                                                │
│   524 mentions · 89% success rate                       │
└─────────────────────────────────────────────────────────┘
```

## Detección de patrones anómalos

El orchestrator alerta si:

- **>10 failures en 1h** → algo está mal (auth roto? token expiró?)
- **0 success en 4h con mentions activas** → engine atascada
- **>200 mentions en 5h** → sospecha de loop, pausa automática

## Acciones manuales para el CEO/CTO

```bash
# Ver quota observation
orclaw quota show

# Forzar pausa del orchestrator
orclaw pause

# Reanudar
orclaw resume

# Ver runs recientes
orclaw runs list --limit 20

# Forzar análisis de saturación ahora
orclaw quota check
```

## Trade-off honesto

Este modelo **NO da paralelismo masivo**. Si Pro plan se vuelve cuello de botella crítico (queremos 10x velocidad), la única salida es:

1. Migrar implementer + reviewer a API key (~50-100 €/mes)
2. Mantener specialist en Pro (sigue siendo cómodo)

La engine está diseñada para que este switch sea **un cambio de config**, no un rewrite. Los prompts viven en `prompts/`, los modelos en `config/`. Si quisieras migrar, solo cambias:

- `config/auth.toml`: añadir `anthropic_api_key_env = "ANTHROPIC_API_KEY"`
- Orchestrator detecta la API key y usa SDK directo en vez de postear `@claude`
- Resto del flujo es idéntico

Pero **mientras no haya señal clara de cuello de botella**, Pro plan + impersonación es lo correcto.
