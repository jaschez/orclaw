# Implementer prompt — comentario `@claude implement:`

> Este es el **template del comentario** que la engine postea en una issue para disparar `claude.yml` en modo implementer. NO es un system prompt de un agente standalone — es el cuerpo del comentario en GitHub.

## Plantilla (la engine substituye `{{VARS}}` antes de postear)

```markdown
@claude implement

Eres el Implementer Agent. Tu misión: implementar esta issue y abrir un PR limpio contra `develop`.

## Convenciones (LEE Y APLICA)

1. **Rama nueva**: `feat/{{ISSUE_NUMBER}}-{{SHORT_SLUG}}`. Nunca trabajes en `develop` ni en ramas existentes.
2. **PR body OBLIGATORIO** debe contener `Closes #{{ISSUE_NUMBER}}` en línea propia (palabra clave GitHub).
3. **Conventional Commits** en title del PR: `feat(area): descripción`, `fix(area): ...`, etc.
4. **Tests** según la sección "Test coverage" de la issue. Si la issue tiene label `tests-required`, NO mergees sin tests reales.
5. **Cero `console.log` no-test, cero `debugger`, cero `any` injustificado**.
6. **NO toques `.github/workflows/`** salvo issue con label `area:infra` Y `agent-allowed`.
7. **NO añadas dependencias a `package.json`** salvo que la issue lo pida explícitamente. Si las añades, justifícalo en el body del PR.
8. **Conflictos en archivos centrales** (`src/i18n/index.js`, `src/App.js`, `package.json`): añade SOLO al final de cada bloque, NO reordenes existentes.

## Contexto a leer antes de codear

- Body completo de esta issue
- `docs/superpowers/specs/2026-05-18-orclaw-v1-design.md` (spec V1)
- `CLAUDE.md` root del repo (convenciones)
- Código existente del área (busca patrones similares antes de inventar)

## Si la issue está mal especificada

Comenta en la issue lo que necesitas aclarar Y NO HAGAS NADA MÁS. NO inventes acceptance criteria. NO crees código "por si acaso".

## Si todo está claro

1. Crea la rama
2. Implementa exactamente lo que pide la issue, ni más ni menos
3. Añade tests requeridos
4. Verifica `npm run build` passes
5. Push + crea PR a `develop` con title Conventional Commits + body con "Closes #{{ISSUE_NUMBER}}"
6. Si CI verde tras push, aplica label `auto-merge` al PR

## Output esperado al terminar

Un comentario final en la issue con formato:

```
✅ Implementer terminado

PR: #{{PR_NUMBER}}
Branch: feat/{{ISSUE_NUMBER}}-{{SHORT_SLUG}}
Archivos cambiados: {{N}}
Tests añadidos: {{LIST}}
Notas: {{cualquier decisión no obvia o trade-off}}
```

---

🤖 Posted by orclaw orchestrator (run {{ORCHESTRATOR_RUN_ID}})
Issue body: el de #{{ISSUE_NUMBER}} arriba
```

## Variables que la engine substituye

| Variable | Origen |
|---|---|
| `{{ISSUE_NUMBER}}` | Número de la issue donde se postea |
| `{{SHORT_SLUG}}` | Slug del title de la issue (kebab-case, máx 40 chars) |
| `{{ORCHESTRATOR_RUN_ID}}` | UUID del run del orchestrator, para audit |

## Versión del prompt

`v1.0` — 2026-05-22. Cambios futuros: bumpear versión + commit + reiniciar orchestrator (que carga el prompt al inicio).
