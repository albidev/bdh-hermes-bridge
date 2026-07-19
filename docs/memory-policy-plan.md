# Piano operativo — Memoria Hermes a due livelli (Honcho + BDH)

> Canonical plan issue: https://github.com/albidev/bdh-hermes-bridge/issues/5
> Vault mirror: `~/Documents/Hermes/wiki/plans/two-tier-memory-honcho-bdh.md`

**Obiettivo:** separare *user model* (Honcho) e *project knowledge* (BDH) con un
contratto di ownership / scope / provenance, senza toccare il core Hermes.

**Architettura:**

```
Hermes MemoryManager
   └── Honcho            = user profile / preferenze / pattern
BDH bridge plugin
   ├── read  (pre_llm_call, già fatto)
   └── write (post_api_request, da vincolare)
Policy/contract layer
   └── decide ownership + scope + provenance
```

**Vincolo fondamentale:** `MemoryManager` ammette un solo external provider →
BDH **non** va registrato come secondo `MemoryProvider`. Si agisce nel bridge.

---

## Fase 0 — Memory contract (senza cambiare comportamento)

Repo `bdh-hermes-bridge`, nuovo modulo `memory_contract.py`.

- Enum tipi: `user_profile`, `project_fact`, `project_decision`,
  `project_concept`, `project_lesson`, `policy_constraint`, `episodic_only`,
  `discard`.
- Struttura candidato: `kind`, `content`, `scope{type,id}`, `owner`,
  `confidence`, `source{session,platform,message,timestamp}`, `status`.
- Regola: `owner` = dove può diventare memoria; `scope` = quanto può essere
  riusato.
- Test: classificazione positiva / negativa / conflitto.

## Fase 1 — Modalità `observe-only`

- Config nel bridge: `memory_policy.mode: observe`, log dei candidati, nessuna
  nuova scrittura.
- Misurare alcuni giorni di uso reale:
  - decisioni riconosciute vs perse;
  - preferenze erroneamente inviate a BDH;
  - rumore promosso;
  - casi `unknown`.
- **Go/No-go:** solo dopo i dati reali si passa a enforce.

## Fase 2 — Enforce write path

- `post_api_request` invia a BDH **solo** `project_*` (fact / decision /
  concept / lesson).
- Honcho continua a ricevere il turno per il proprio user model.
- `policy_constraint` → config / policy, non memoria semantica.
- `episodic_only` → session history. `discard` → ignorato.
- Fix: la soglia di lunghezza non basta; serve classificazione semantica prima
  della scrittura BDH.

## Fase 3 — Read path esplicito

- Due blocchi separati nel prompt: `USER CONTEXT` (Honcho) e
  `PROJECT KNOWLEDGE` (BDH). Niente blocco unico `memory-context`.
- Precedenza: policy > decisione scoped > fatto con provenance > preferenza
  globale > inferenza debole.
- Decisione scoped prevale su preferenza generale dell'utente.

## Fase 4 — Provenance su BDH

- `bdh-graph-harness`: il write path accetta `scope`, `kind`, `source`,
  `status`.
- Note con tipo esplicito: `decision` / `fact` / `lesson` / `concept`.
- Stato `superseded` per decisioni obsolete (tombstone, non delete cieco).

---

## File da toccare

- `bdh-hermes-bridge/memory_contract.py` (nuovo)
- `bdh-hermes-bridge/__init__.py` (applica policy, modalità observe/enforce,
  log routing)
- `bdh-hermes-bridge/test_memory_contract.py` (nuovo)
- `bdh-hermes-bridge/test_bdh_bridge.py` (estendi con routing + conflitto)
- `bdh-graph-harness`: note con metadata; nessun nuovo servizio / DB.

**Hermes core:** nessuna modifica in Fase 0–4.

---

## Test minimi obbligatori

- **Positivi:** "abbiamo deciso X" → `project_decision` in BDH; "il watcher
  ricostruisce BM25" → `project_fact`.
- **Negativi:** "preferisco risposte concise" → Honcho; "ciao grazie" →
  `discard`; "mi ha fatto perdere tempo" → mai BDH.
- **Conflitto:** preferenza globale Honcho vs decisione scoped BDH → vince BDH.
- **Provenance:** ogni memoria promossa deve rispondere a sessione / messaggio /
  scope / confidence / status.

## Criteri di successo

- Honcho senza regressioni.
- BDH non riceve più preferenze generiche.
- Decisioni tecniche nel vault con provenance.
- Query casuali non attivano scritture.
- Un progetto non inquina l'altro vault.
- Il sistema spiega perché un candidato è stato scritto o scartato.

## Ordine di esecuzione

1. Memory contract
2. Observe-only
3. Test su conversazioni reali
4. Enforce BDH
5. Metadata note BDH
6. Conflict resolution
7. (solo dopo) eventuale estensione core Hermes

Prima modifica concreta: **router di ownership osservabile** davanti alle
scritture BDH, con Honcho / Hermes intatti finché non abbiamo i dati sui casi
ambigui.
