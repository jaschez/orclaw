# Operations — Day-to-day playbook

Tu manual cuando la engine está en producción.

## 1. Cómo trabajar con la engine en un día normal

```bash
# Inicio de jornada (~5 min)
ssh engine@<server>
orclaw status                      # vista rápida del estado actual
# o desde el navegador: https://engine.${TARGET_REPO}/status

# Hablar con el Specialist sobre lo siguiente a construir
orclaw specialist
> [conversación sobre la nueva feature]
> sí, procede a crear los issues

# Resto del día: olvídate
# La engine ejecuta batches, opens PRs, los reviewer agents revisan, auto-merge mergea.
# Tu único toque humano: si llega un PR a "needs-changes" o si un OPS te toca.

# Fin del día
orclaw status                      # qué se cerró hoy, qué OPS pendientes tienes
```

## 2. Cuándo TÚ tienes que actuar

| Situación | Qué hacer |
|---|---|
| OPS issue marcada (etiqueta `ops`) | Ejecutar en tu local con los MCPs/CLIs (InsForge, Stripe, Vercel) |
| PR con label `needs-changes` | Lee el feedback del reviewer agent, decide si lo arreglas tú o re-pides al specialist que ajuste la issue |
| PR con label `requires-human-review` | Tu review humano antes de mergear |
| Issue automática `engine:budget-hard-stop` | Decide: pagar más este mes o esperar al próximo, después `orclaw budget resume` |
| Issue automática `engine:dep-cycle-detected` | Edita los bodies de las issues implicadas para romper el ciclo |
| Issue automática `engine:implementer-failed-3x` | Revisar el log de los runs fallidos. Probablemente la issue está mal definida o falta contexto |
| Server caído (alerta de Healthchecks) | SSH, `journalctl -u orclaw-orchestrator -n 100`, decide acción |

## 3. Comandos típicos del CLI `orclaw`

```bash
orclaw status                              # dashboard ASCII en terminal

orclaw specialist                          # entra en modo conversacional
orclaw specialist --resume <id>            # retoma una conversación previa

orclaw planner run                         # fuerza recálculo del batch ahora
orclaw planner show                        # muestra layers + estado actual

orclaw batch start                         # spawn implementer del batch actual (si no auto)
orclaw batch cancel                        # abort batch en curso

orclaw implementer logs <issue_number>     # logs de la implementación de #N
orclaw implementer retry <issue_number>    # fuerza retry

orclaw reviewer review <pr_number>         # corre el reviewer manual sobre un PR

orclaw budget show                         # estado del budget mensual
orclaw budget pause                        # detiene nuevos spawns
orclaw budget resume                       # reanuda
orclaw budget set --monthly-eur 80         # actualiza target

orclaw config edit                         # abre config en $EDITOR
orclaw config validate                     # valida config sin aplicar
```

## 4. Pausas controladas

Si por cualquier motivo necesitas pausar la engine (vacaciones, debugging, presupuesto agotado):

```bash
# Pausa suave: deja que los batches en vuelo terminen, no arranca nuevos
orclaw pause

# Pausa dura: aborta todo en vuelo también
orclaw pause --hard

# Reanuda
orclaw resume
```

Lo que estaba en vuelo se reanuda donde lo dejó (si pausa suave) o se marca como `pending` para reintentar (si pausa dura).

## 5. Anti-patterns que la engine NO hace

Para que sepas qué esperar:

- **No mergea PRs marcados `do-not-auto-merge` o `requires-human-review`** — pasan por ti
- **No abre PRs sobre issues con label `ops` o `do-not-implement`**
- **No toca issues etiquetadas `wontfix` o cerradas**
- **No modifica workflows en `.github/workflows/`** salvo que la issue esté etiquetada `infra` Y `agent-allowed`
- **No commitea secretos** (hard check en reviewer)
- **No re-fuerza branches** (force push prohibido en cualquier rama que no sea su propia rama de PR)
- **No mergea a `main`** — solo a `develop`

## 6. Cómo el Specialist te plantea decisiones

El Specialist es conversacional pero estructurado. Outputs típicos:

**A) Conversación de scoping**

> Vamos a ver. He revisado el spec V1 + las issues abiertas. Antes de generar issues nuevas para "X", necesito que decidas:
>
> 1. ¿Aplica a eventos de pago, gratuitos, o ambos?
> 2. ¿Es obligatorio o opcional para el organizador?
> 3. ¿Quieres analytics asociado?
>
> Si quieres lo más rápido, responde "default" y yo elijo lo más natural según el resto del producto.

**B) Plan de issues**

> Plan generado: 4 issues + 1 OPS, 2 batches.
>
> **Batch 1** (paralelo, sin deps):
> - #201 [P0] coupons table + RLS + admin RPC
> - #202 [P0] frontend: coupon code input en checkout
>
> **Batch 2** (depende del 1):
> - #203 [P0] stripe-create-ticket-checkout: aplicar cupón
> - #204 [P1] /manage/[event]/coupons: UI CRUD
>
> **OPS** (TUYO):
> - #205 [OPS] aplicar migración (`npx @insforge/cli db import migrations/...`)
>
> Coste estimado: ~$2.50 en tokens. Tiempo total estimado: 1.5-2h reales.
>
> ¿Procedo a crearlos? [yes/edit/abort]

**C) Aviso de riesgo**

> Atención: la feature que propones toca el flujo de pago de Stripe. Para esa zona el reviewer agent NO aplica auto-merge — vas a tener que aprobar tú los PRs manualmente.
>
> Esto se debe a la etiqueta `requires-human-review` que aplico automáticamente sobre cambios en:
> - `insforge/functions/stripe-*`
> - `src/services/payment*`
> - cambios al schema de `tickets` o `events`
>
> ¿Procedo igual?

## 7. Cómo el Reviewer Agent te habla

El reviewer comenta SIEMPRE en el PR. Outputs típicos:

**A) Aprobado**

> ✅ **Reviewer Agent: approved**
>
> Checklist programático:
> - ✓ Closes #201 en body
> - ✓ Tests añadidos: `coupons.test.ts`, `useCoupons.test.jsx`
> - ✓ No secrets, no console.log, no debugger
>
> Análisis cualitativo:
> - Cumple los acceptance criteria del issue
> - Edge cases cubiertos: código vencido, código sin usos, código aplicado dos veces (rechazado en tests)
> - Estilo coherente con el resto de hooks
>
> Aplicando label `auto-merge`. El workflow lo mergeará cuando CI esté verde.

**B) Cambios pequeños arreglados**

> 🔧 **Reviewer Agent: minor fixes applied**
>
> Encontré dos cosas menores que arreglé directamente:
> - Faltaba aria-label en el input del cupón → commit `a1b2c3d`
> - Test snapshot rota tras el cambio → regenerado en commit `e4f5g6h`
>
> Re-evaluando... ✅ approved. Aplicando `auto-merge`.

**C) Necesita tu mano**

> ⚠️ **Reviewer Agent: needs-changes**
>
> Bloqueante:
> - El PR introduce uso directo de `process.env.STRIPE_SECRET_KEY` en el frontend (`src/services/coupons.js:42`). Esto rompe la separación servidor/cliente y filtra el secreto al bundle.
>
> Sugerencias:
> - Mover la operación a una edge function nueva o usar la existente `stripe-create-ticket-checkout` con un nuevo parámetro.
>
> Aplicando label `needs-changes`. NO he aplicado `auto-merge`. Decide: ¿re-pides al specialist que ajuste la issue para que el implementer lo haga bien, o lo arreglas tú directamente?

## 8. Healthchecks que deberías tener

Recomendación: cuenta gratis en [healthchecks.io](https://healthchecks.io). Crea checks:

| Check | Frecuencia | Qué notifica |
|---|---|---|
| `orclaw-orchestrator alive` | cada 5 min | el orchestrator pinga el ping URL |
| `orclaw-backup daily` | cada 24 h | el backup script pinga al terminar |
| `orclaw-batch-planner` | cada 15 min | el planner pinga después de cada run |

Notification: email + Slack si tienes. Si un check no pinga en su ventana, recibes alerta.

## 9. Auditoría / compliance

Por si en algún momento quieres saber quién/qué hizo qué:

- **Toda llamada a la API queda en `token_ledger`** (qué agente, qué issue/PR, cuándo, cuánto)
- **Todo run de implementer queda en `runs` table** (input, output, status, branch, commits)
- **Todo cambio de estado de batch queda en `batch_history`**
- **GitHub events ya guarda commits + PRs + comentarios**

Para audit "qué se hizo en mayo":

```sql
SELECT date(started_at) AS day,
       SUM(cost_usd_cents)/100.0 AS usd,
       COUNT(DISTINCT issue_number) AS issues_worked,
       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes
FROM token_ledger
WHERE started_at >= '2026-05-01' AND started_at < '2026-06-01'
GROUP BY day
ORDER BY day;
```

## 10. Cuándo intervenir manualmente (override)

Casi nunca. Pero a veces:

- **Necesitas que una issue se haga YA**, sin esperar al planner: `orclaw implementer start <issue_number>` (salta cola)
- **Sabes que el reviewer va a rechazar pero quieres mergearlo igual** (rara vez, p.ej. spike experimental): añade label `force-merge` al PR + comenta justificación en el PR. La engine acepta `force-merge` solo si tú eres el autor del comentario (no un agente)
- **Quieres testear un cambio al prompt del specialist sin riesgo**: `orclaw specialist --dry-run` no crea issues, solo te muestra qué crearía

## 11. Anti-loops de seguridad

La engine NO permite:

- Crear más de 100 issues en una sesión del Specialist (límite contra runaway)
- Spawnar más de 10 implementers simultáneos (hard cap por encima del budget cap)
- Re-implementar la misma issue más de 3 veces sin tu intervención
- Mergear un PR cuyo último commit lo hizo el reviewer agent (anti circular self-approval) — el reviewer puede committear fixes pero NO puede ser el último committer del merge

Si alguno de estos triggers se dispara, alerta + pausa automática.
