# bdh-hermes-bridge

Bidirectional bridge between [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [BDH Graph Harness](https://github.com/albidev/bdh-graph-harness).

Feeds Hermes session knowledge into BDH's neural graph, and exposes BDH context as tools Hermes can query.

## About

**BDH Hermes Bridge** is a plugin that connects Hermes Agent (an autonomous AI assistant) with BDH Graph Harness (a knowledge graph implementing biological neural network analogies from the [Dragon Hatchling paper](https://arxiv.org/abs/2509.26507)).

The core idea: every conversation with Hermes becomes training data for a living knowledge graph. The graph learns from real usage — not fabricated queries — and grows organically through:

- **Hebbian learning** — neurons that fire together wire together
- **Neurogenesis** — new concepts born from conversation gaps
- **Quality propagation** — activation scores ripple through the network
- **Dormancy** — unused connections fade, keeping the graph clean

Hermes, in turn, can query the graph for context it wouldn't otherwise have — surfacing connections between projects, past decisions, and technical knowledge buried in the vault.

**This isn't a RAG pipeline.** It's a neural memory system that learns *what matters* by observing real interactions.

### Key concepts

| Concept | Description |
|---------|-------------|
| Neurons | Vault notes — atomic knowledge units |
| Wikilink synapses | `[[wiki/links]]` between notes |
| Hebbian synapses | Learned associations from co-activation |
| Neurogenesis | New note creation when the graph detects knowledge gaps |
| Dormancy | Low-quality nodes marked for pruning |

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
