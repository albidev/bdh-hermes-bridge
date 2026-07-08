<p align="center">
  <img src="cover.png" alt="BDH Hermes Bridge" width="100%">
</p>

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

#### Echo-loop prevention

The write path sends `source: "assistant_response"` in the payload. BDH applies dampened Hebbian learning (frequency += 0.3 instead of 1.0) to prevent feedback amplification where Hermes echoes BDH context back into the graph.

#### User context capture

The `pre_llm_call` hook captures the last user message, which is included in write payloads alongside the assistant response. This creates proper question→answer synaptic associations that would otherwise be lost.

### Read path — BDH → Hermes

Two tools registered for Hermes to query the knowledge graph:

| Tool | Description |
|------|-------------|
| `bdh_query` | Query BDH with a question. Returns activated neurons, LLM response, and any new concepts created. |
| `bdh_stats` | Quick graph stats: neuron count, active/dormant, synapses, hebbian links. |

When BDH is unreachable (e.g. during sleep-cycle consolidation), `bdh_query` returns a graceful fallback message instead of crashing the agent.

### Resilience

All BDH HTTP requests use retry with exponential backoff (3 attempts: 1s, 2s, 4s). This handles transient failures during BDH's consolidation cycles without dropping data.

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
  │ pre_llm_    │──► captures user_message
  │ call hook   │
  └──────┬──────┘
         ▼
  ┌─────────────┐
  │ Hermes LLM  │ ◄── bdh_query tool (if Hermes needs graph context)
  └──────┬──────┘
         │ response (>200 chars)
         ▼
  ┌──────────────┐
  │ post_api_    │──► sends {query, user_prompt, source: "assistant_response"}
  │ request hook │
  └──────┬───────┘
         │ fire-and-forget (with retry)
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
 (dampened     │
  for LLM      │
  responses)   │
```

The graph grows organically from real usage — no fabricated queries, no noise.

## Hooks used

| Hook | When | Purpose |
|------|------|---------|
| `pre_llm_call` | Before LLM generates | Capture user message for write path context |
| `post_api_request` | After API response (stop) | Feed assistant response + user context to BDH |

## License

MIT
