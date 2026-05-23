# Pro-plan-only execution strategy

## Decisión

**100 % del trabajo de los agentes Claude pasa por el plan Pro**, vía el workflow `claude.yml` ya existente en `${TARGET_REPO}`. **Cero uso de la API de Anthropic con API key**.

El mecanismo concreto:

1. La engine (server remoto) actúa como dispatcher: decide qué hacer, cuándo, dónde, con qué prompt.
2. Para cada acción, la engine **postea un comentario en un issue o PR** mencionando `@claude` + las instrucciones.
3. El comentario aparece como **autoría del CEO/CTO (${GITHUB_USERNAME})** mediante un Personal Access Token con scope `repo`. Esto es legítimo: el CEO/CTO autoriza explícitamente al server a actuar en su nombre dentro de su propio repo privado.
4. GitHub dispara el workflow `claude.yml` (ya existente).
5. Claude responde usando el `CLAUDE_CODE_OAUTH_TOKEN`, atado al plan Pro.
6. Todo el trabajo (implementación, review, planificación de issues) se ejecuta dentro de la cuota Pro, sin coste extra.

## Por qué este modelo

- **Coste extra = 0 €**. La hipótesis previa de usar API key suponía ~20-100 €/mes. Eliminado.
- **Un único punto de auth con Anthropic** (OAuth Pro), ya configurado y validado.
- **Sin lógica de tracking de tokens en cents** — la cuota es la Pro, no hay factura.
- **Reaprovecha workflows existentes** (claude.yml, cleanup-agent-start.yml, auto-merge.yml).

## Lo que sacrificamos a cambio

| Limitación | Mitigación |
|---|---|
| Pro plan ~225 msg/5h compartidos entre TODA actividad | Concurrency cap real = 1-2 en vuelo. Sequential by design |
| Sin visibilidad directa del % de cuota Pro restante | Observamos indirectamente (workflow durations, 429s, conclusion=failure con auth errors) |
| Si tu uso interactivo + engine coinciden, te bloqueas a ti mismo | El specialist agent también pasa por @claude → minimiza conflicto |
| Más latencia (claude.yml tarda ~10-30 s en arrancar después del comentario) | Aceptable para nuestro caso. No es real-time |
| No paralelismo masivo: aunque postemos 5 @claude comments a la vez, el backend de Claude rate-limita | Engine respeta el cap, no inunda. Si Pro bloquea, retry exponencial |

## Cómo funciona el ciclo, paso a paso

### Fase 1 — Planificación (Specialist)

1. CEO/CTO crea / reabre la **issue de specialist conversation** (p.ej. una issue dedicada `[META] Specialist conversation`).
2. Comenta su idea/requisito en lenguaje natural. **Sin** `@claude` (esto es para el CEO/CTO, no dispara nada).
3. Cuando está listo, comenta: `@claude specialist: actúa como agente especialista, analiza y propón plan`.
4. claude.yml fires → Claude lee el thread completo + spec del repo → responde con plan estructurado.
5. CEO/CTO confirma con un comentario `@claude proceed` o edita los ajustes pedidos.
6. Claude crea las issues con `gh` CLI desde dentro del runner del Action.

Alternativa más simple: la engine ofrece un CLI local `orclaw specialist` que es solo un wrapper que postea por ti el comentario en la issue meta y lee la respuesta. Misma cosa con mejor UX.

### Fase 2 — Implementación (Implementer)

1. Batch Planner (en el server) recalcula qué issues están listas.
2. Orchestrator selecciona N (con N pequeño, 1-2) del batch actual.
3. Para cada una, postea un comentario en la issue:
   ```
   @claude implement: implementa esta issue siguiendo los acceptance criteria. Spec en docs/superpowers/specs/. Branch feat/<num>-<slug>. Body del PR con "Closes #N". Aplica label auto-merge si CI verde.
   ```
4. claude.yml fires → Claude implementa → abre PR a develop.
5. Orchestrator observa el evento `pull_request.opened` y pasa a fase 3.

### Fase 3 — Review

1. Orchestrator detecta PR abierto que cierra una issue del batch.
2. Postea comentario en el PR:
   ```
   @claude review: actúa como reviewer agent (ver prompts/reviewer.md en orclaw). Aplica hard checks + análisis cualitativo. Decide approved / needs-changes.
   ```
3. claude.yml fires → Claude lee PR + acceptance criteria + spec → comenta con verdict.
4. Si approved → Claude aplica label `auto-merge` (ya tiene permisos via OAuth).
5. auto-merge.yml mergea cuando CI verde.
6. cleanup-agent-start.yml quita `agent:start` cuando la issue se cierra.

### Fase 4 — Avance

1. Orchestrator detecta que TODAS las issues del batch están `merged` o `failed`.
2. Avanza al siguiente layer.
3. Recurre fase 2-3.

## Concurrencia real

Dado que TODO pasa por una única OAuth token / Pro plan:

- **Hard limit: 2 invocaciones a `@claude` en vuelo simultáneo**. Más es contraproducente (rate limit → fails → retry → quota gastada).
- En caso de duda, **secuencial**. Es lento, pero predecible.
- Si Pro plan reset acaba de pasar (cada 5h ventana rolling), podemos hacer mini-burst de 2-3 acciones rápidas y luego volver a 1.

## Cómo medimos la cuota sin acceso directo

Anthropic no expone el % restante del plan Pro. Lo inferimos:

1. **Latencia de workflow runs**: si claude.yml tarda > 5 min en arrancar (queue), Pro está saturado.
2. **Conclusion=failure con auth error**: el OAuth token devuelve 401/429 cuando se agota → log en `engine.db.runs` con `status='rate_limited'`.
3. **Frecuencia de runs últimas 5 h**: aproximación a la cuota consumida. La engine cuenta runs (no tokens, no podemos).

Cuando detectamos saturación:

- Esperar 5 min, reintentar
- Si 3 retries fallan → marcar el slot como `wait_for_quota`
- Notificar al CEO/CTO ("cuota Pro saturada, espera ~30-60 min")
- Reanudar cuando la siguiente acción de prueba devuelva éxito

## Impersonación: el PAT del CEO/CTO

La engine necesita un PAT clásico de GitHub bajo la cuenta `${GITHUB_USERNAME}` con scopes:

- `repo` (full): para comentar issues/PRs, leer state, aplicar labels
- `project`: para mover cards en Project V2
- `workflow`: por si necesita disparar workflow_dispatch
- `read:org`: opcional, si vamos a usar features de organización

Este PAT lo guardas como `GITHUB_TOKEN` en `/etc/orclaw/secrets.env` del server.

**Riesgo**: si alguien comprometiera el server, podría comentar como `${GITHUB_USERNAME}` y disparar runs de Claude consumiendo la cuota Pro. **Mitigación**:

- Server hardenizado (SSH key only, ufw, no root, no password)
- PAT con expiración explícita (90 días, renovar)
- Healthchecks que detecten actividad anómala (>X comments/h)
- Kill switch: cerrar tracker issue #131 → engine detiene todos los `@claude` mentions

## El claude.yml side: ¿algo que cambiar?

`${TARGET_REPO}/.github/workflows/claude.yml` queda como está. Acepta:

- `@claude` en comentarios de issues
- `@claude` en comentarios de PRs (review)
- `@claude` en el body al abrir/asignar una issue

El **prompt** que Claude recibe depende del **contenido del comentario**. Por eso es crítico que la engine postee comentarios bien estructurados (ver siguiente sección).

## Convención de comentarios que postea la engine

Cada comentario tiene una etiqueta de modo al principio que el agente reconoce:

```
@claude implement: <prompt extendido del implementer>
@claude review: <prompt extendido del reviewer>
@claude specialist: <prompt extendido del specialist>
@claude triage: <prompt para clasificar issues huérfanas>
```

El prompt extendido vive en `prompts/*.md` en este repo y se inyecta literal al postear. Versionado, auditable.

Ejemplo de comentario que postea la engine para implementar:

```markdown
@claude implement

Sigue las instrucciones del agente implementer descritas en
`prompts/implementer.md` del repo orclaw. Resumen:

- Lee body completo de esta issue + spec en docs/superpowers/specs/2026-05-18-orclaw-v1-design.md
- Rama nueva: feat/<NUM>-<slug>
- PR body OBLIGATORIO: "Closes #<NUM>"
- Tests según sección "Test coverage" de la issue
- Si CI verde tras push, aplica label `auto-merge`
- NO toques .github/workflows/ salvo issue con label `area:infra` Y `agent-allowed`

Issue: #142
Spec: ${TARGET_REPO}/docs/superpowers/specs/2026-05-18-orclaw-v1-design.md
Convenciones: ${TARGET_REPO}/CLAUDE.md

---
🤖 Posted by orclaw orchestrator (run 8a2f...)
```

Estructura repetible. Auditable. Pasa el filtro `@claude` de claude.yml.

## Resumen del modelo final

```
       orclaw SERVER                ${TARGET_REPO} REPO + GH Actions
       ────────────────────                ──────────────────────────

   ┌──────────────────────┐
   │ Specialist (CLI o    │ ───┐
   │ Issue meta-thread)   │    │
   └──────────────────────┘    │ posts @claude specialist:
                                │
   ┌──────────────────────┐    │
   │ Batch Planner        │    │
   │ (algoritmo puro)     │    │
   └──────────┬───────────┘    │
              │                 │
              ▼                 │
   ┌──────────────────────┐    │
   │ Orchestrator         │ ───┴──────┐
   │ (state machine)      │            │ posts @claude implement:
   └──────────┬───────────┘            │ posts @claude review:
              │                         │
              │                         ▼
              │                  ┌─────────────────────┐
              │                  │ claude.yml          │
              │                  │ (OAuth, Pro plan)   │
              │                  │ - implementer       │
              │                  │ - reviewer          │
              │                  │ - specialist        │
              │                  └──────────┬──────────┘
              │                              │
              │                              ▼
              │                  ┌─────────────────────┐
              │                  │ Claude responde     │
              │                  │ - opens PR          │
              │                  │ - comments review   │
              │                  │ - applies labels    │
              │                  └──────────┬──────────┘
              │                              │
              │                              ▼
              │                  ┌─────────────────────┐
              │                  │ auto-merge.yml      │
              │                  │ cleanup-agent-start │
              │                  └──────────┬──────────┘
              │                              │
              │     observa via webhooks/poll│
              └──────────────────────────────┘
```

## Open questions todavía

1. **Modo del Specialist**: ¿CLI local invocado desde tu máquina vs issue meta-thread en GitHub? El segundo es 100 % pro-only-vía-GitHub, pero la UX es más lenta. ¿Eliges uno?
2. **Webhooks vs polling**: ¿La engine recibe webhooks de GitHub (requiere endpoint público) o hace polling cada N segundos (más simple, peor latencia)?
3. **Vida del PAT**: 90 días con renovación manual recordatoria, o sin expiración? (Recomiendo 90d.)
