# bdh-hermes-bridge

Bidirectional bridge between [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [BDH Graph Harness](https://github.com/albidev/bdh-graph-harness).

Feeds Hermes session knowledge into BDH's neural graph, and exposes BDH context as tools Hermes can query.

## What it does

### Write path — Hermes → BDH

After every substantial LLM response (`post_api_request` hook, `finish_reason == "stop"`), the plugin sends the response content to BDH's `/api/query` endpoint. This triggers:

- **Hebbian reinforcement** — co-activated neurons strengthen their synaptic links
- **Neurogenesis** — if the LLM response contains genuinely new concepts, BDH creates atomic notes in the vault
- **Quality updates** — activation scores propagate through the graph

Only fires on responses >200 chars (skips trivial acks like "done", "pushato").

### Read path — BDH → Hermes

Two tools registered for Hermes to query the knowledge graph:

| Tool | Description |
|------|-------------|
| `bdh_query` | Query BDH with a question. Returns activated neurons, LLM response, and any new concepts created. |
| `bdh_stats` | Quick graph stats: neuron count, active/dormant, synapses, hebbian links. |

## Requirements

- [BDH Graph Harness](https://github.com/albidev/bdh-graph-harness) running on `localhost:8643`
- Hermes Agent with plugin support (gateway mode)

## Install

```bash
# Clone
git clone https://github.com/albidev/bdh-hermes-bridge.git ~/.hermes/plugins/bdh-hermes-bridge

# Or symlink from a dev repo
ln -s ~/Projects/bdh-hermes-bridge ~/.hermes/plugins/bdh-hermes-bridge
```

Enable in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - bdh-hermes-bridge
```

Restart the gateway:

```bash
hermes gateway restart
```

## How it works

```
User asks Hermes a question
        │
        ▼
  ┌─────────────┐
  │ Hermes LLM  │ ◄── bdh_query tool (if Hermes needs graph context)
  └──────┬──────┘
         │ response (>200 chars)
         ▼
  ┌──────────────┐
  │ post_api_    │
  │ request hook │
  └──────┬───────┘
         │ fire-and-forget
         ▼
  ┌──────────────┐
  │ BDH /api/    │
  │ query        │
  └──────┬───────┘
         │
    ┌────┴────┐
    ▼         ▼
 Hebbian   Neurogenesis
 learning  (if new concepts)
```

The graph grows organically from real usage — no fabricated queries, no noise.

## License

MIT
