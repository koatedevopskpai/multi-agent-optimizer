> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Phase 2 LangChain/LangGraph boundary decision

Date: 2 November 2026. Status: retained custom adapter for the offline slice; revisit after an authorised provider capability run.

## What is native

- **LangGraph** `StateGraph`, conditional edges, `interrupt()`/resume and the `MemorySaver` checkpointer are used natively for the workflow.
- The existing `multi_agent_optimizer` package is unchanged.

## What is custom

- `ChatMessage`, `ToolCall`, `ProviderRequest` and `ProviderResponse` are small Pydantic contracts owned by this project.
- `ProviderRunner` owns retry classification, transport limits, circuit breaking and fallback accounting.
- `OpenAICompatibleProvider` serialises the chat/tool conversation and parses tool-call IDs.
- `TraceRecorder` is a local span recorder. `LangSmithTraceExporter` is a disabled-by-default seam; `LangSmithClientSink` is a concrete adapter for an injected LangSmith-compatible client. No client is constructed or called by default.
- Tools are deterministic Python callables, not LangChain tool objects.

## Why retain the custom adapter for now

1. The offline tests need byte-level control of tool-call IDs, structured assistant/tool messages and malformed responses.
2. The project must prove that retries, deadlines and token admission are bounded outside the model SDK. A custom boundary makes hidden SDK retries visibly impossible.
3. No provider access or spending scope is established, so a native LangChain provider adapter cannot be capability-tested against a real model.
4. LangGraph is already native; adding LangChain model abstractions now would add surface area without an evaluated benefit.

## Revisit criteria

After provider access is authorised:

- compare the custom adapter with a supported `langchain-openai` or `langchain-aws` model/tool integration;
- verify tool-call IDs, multiple tool calls, structured messages and usage reporting on the actual provider;
- retain the deterministic fake-provider suite as the offline contract test;
- do not replace the custom adapter unless the native integration preserves the tool-message contract and bounded-retry evidence;
- record the decision as an ADR with observed results, not preference alone.

## LangSmith boundary

The local trace spans remain authoritative for offline evidence. The exporter seam is disabled by default and accepts an injected sink; the concrete `LangSmithClientSink` maps local node/provider/tool kinds to LangSmith run types, converts UTC timestamps and forwards sanitised metadata. Tests cover parent/child linkage, run types, timestamps, sanitisation and sink-failure handling without network calls. The workflow accepts an optional exporter for offline wiring. Live LangSmith export and workspace addressing remain pending credentials and trace-export scope.
