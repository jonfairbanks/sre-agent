# SRE Bot architecture

How the pieces fit together, covering entry points, the two request paths, the
subagent fan-out, and the human-in-the-loop approval cycle.

Rendered PNGs of each diagram live alongside this file for use outside GitHub,
in slides, the blog post, or anywhere Mermaid does not render. They are
`sre-bot-architecture-simple.png`, `sre-bot-architecture-overview.png`,
`sre-bot-request-paths.png`, `sre-bot-hitl-sequence.png`, and
`sre-bot-cost-safety.png`. Regenerate them with the command below.

```bash
npx -p @mermaid-js/mermaid-cli mmdc -i diagram.mmd -o out.png -b white -s 3
```

## Simplified overview

The version to read first, and the one to put on a slide. It keeps the four
entry points, both request paths, the subagent split, and the approval gate,
and leaves out the storage and observability wiring. Source is
`sre-bot-architecture-simple.mmd`.

```mermaid
flowchart LR
    subgraph entry["Entry points"]
        CLI["main.py<br/>CLI"]
        WEB["api.py<br/>Web UI, REST, SSE"]
        BOLT["Slack Bolt<br/>Socket Mode"]
        SCHED["MonitoringScheduler<br/>every N minutes"]
    end

    ROUTER["Intent router<br/>health checks and audits<br/>take the fast path"]

    subgraph fast["Fast path, scheduler.py"]
        COLLECT["Collect cluster data<br/>direct client, zero tokens"]
        HAIKU["One Haiku call<br/>forced tool use"]
        REPORT["Typed HealthReport"]
    end

    subgraph agent["Agent path, agent.py"]
        ORCH["Orchestrator<br/>Sonnet, plans and delegates"]
        LIM["Guards: prompt caching,<br/>recursion and call limits"]
        RO["Read-only subagents<br/>Haiku, 8 analysts"]
        CE["change-executor<br/>only holder of write tools"]
    end

    GATE{{"Human approval<br/>per write tool call"}}

    subgraph toolset["Tools"]
        RT["44 read tools"]
        WT["11 write tools"]
    end

    K8C["tools/k8s_client.py<br/>in-cluster or local"]
    K8S[("Kubernetes API")]
    SLK["Slack workspace"]

    CLI --> ORCH
    WEB --> ROUTER
    BOLT --> ROUTER
    SCHED --> COLLECT
    ROUTER -->|"health check or audit"| COLLECT
    ROUTER -->|"everything else"| ORCH

    COLLECT --> HAIKU
    HAIKU --> REPORT
    REPORT --> SLK

    ORCH -.- LIM
    ORCH --> RO
    ORCH --> CE
    ORCH --> RT
    RO --> RT
    CE --> GATE
    GATE -->|"approved"| WT
    GATE -.->|"asks, then reports"| SLK
    ORCH --> SLK

    COLLECT --> K8C
    RT --> K8C
    WT --> K8C
    K8C --> K8S

    classDef bounded fill:#e6f4ea,stroke:#2f855a
    classDef write fill:#fde8e8,stroke:#c53030
    class COLLECT,HAIKU,REPORT,SCHED bounded
    class CE,GATE,WT write
```

Note that the CLI bypasses the router and always goes to the orchestrator; only
the web and Slack entry points are routed. Everything below adds detail to this
picture. The component overview adds the checkpointer, filesystem backend, and
the Anthropic and LangSmith edges, the two request paths expand the routing
decision, and the HITL sequence expands the approval gate.

## Component overview

```mermaid
flowchart LR
    subgraph entry["Entry points"]
        CLI["main.py<br/>CLI, rich display"]
        WEB["api.py<br/>Web UI + REST + SSE<br/>port 8080"]
        BOLT["Slack Bolt<br/>Socket Mode thread"]
        SCHED["MonitoringScheduler<br/>every N minutes"]
    end

    ROUTER{"_HEALTH_CHECK_RE<br/>regex intent router"}

    subgraph core["Agent core, agent.py"]
        ORCH["Main orchestrator<br/>create_deep_agent<br/>claude-sonnet-4-6"]
        MW["Middleware: prompt caching,<br/>model and tool call limits"]
        MEM["MemorySaver + InMemoryStore<br/>FilesystemBackend, virtual"]
        ORCH -.- MW
        ORCH -.- MEM
    end

    BOUND["Bounded health check<br/>scheduler.py<br/>collect, then 1 Haiku call"]

    subgraph agents["Subagents"]
        RO["8 read-only analysts<br/>claude-haiku-4-5<br/>pod, scaling, performance, log,<br/>security, reliability, job, config"]
        CE["change-executor<br/>claude-sonnet-4-6<br/>every write tool HITL gated"]
    end

    subgraph toolset["Tools"]
        RT["44 read tools<br/>k8s read, security, reliability,<br/>hygiene, batch, helm read"]
        WT["11 write tools<br/>scale, patch, delete, cordon,<br/>rollout restart, apply"]
        ST["send_slack_notification"]
    end

    K8C["tools/k8s_client.py<br/>in-cluster vs local detection"]

    subgraph ext["External"]
        K8S[("Kubernetes API")]
        SLK["Slack workspace"]
        ANTH["Anthropic API<br/>optional gateway"]
        LS["LangSmith tracing"]
    end

    CLI --> ORCH
    WEB --> ROUTER
    BOLT --> ROUTER
    SCHED --> BOUND
    ROUTER -->|"health check or audit"| BOUND
    ROUTER -->|"everything else"| ORCH

    ORCH -->|"task()"| RO
    ORCH -->|"task()"| CE
    ORCH --> RT
    ORCH --> ST
    RO --> RT
    CE --> RT
    CE --> WT

    RT --> K8C
    WT --> K8C
    BOUND --> K8C
    K8C --> K8S
    ST --> SLK
    BOUND --> SLK

    core -.->|"all model calls"| ANTH
    BOUND -.-> ANTH
    core -.->|"traces"| LS

    classDef write fill:#fde8e8,stroke:#c53030
    classDef bounded fill:#e6f4ea,stroke:#2f855a
    class CE,WT write
    class BOUND,SCHED bounded
```

## Two request paths

A health check and a diagnosis are handled very differently. The distinction
exists because fanning out to subagents can exceed the langgraph recursion
limit, so periodic and on-demand health checks take a path with a fixed step
count instead.

```mermaid
flowchart TB
    IN["User message<br/>or scheduled tick"] --> Q{"matches<br/>_HEALTH_CHECK_RE?"}

    Q -->|yes| B1["_collect_cluster_data<br/>direct kubernetes client<br/>zero LLM tokens"]
    B1 --> B2["_format_snapshot<br/>compact text"]
    B2 --> B3["one claude-haiku call<br/>forced tool use: report_health"]
    B3 --> B4["validated HealthReport<br/>Pydantic, no regex parsing"]
    B4 --> B5["send_structured_report<br/>Block Kit from typed fields"]

    Q -->|no| O1["Main orchestrator<br/>write_todos, plan"]
    O1 --> O2["get_cluster_summary"]
    O2 --> O3["Parallel task() fan-out<br/>to read-only subagents"]
    O3 --> O4["Synthesize free-text report<br/>[CRITICAL] / [WARNING] markers"]
    O4 --> O5["send_health_report<br/>legacy text parsing"]

    classDef bounded fill:#e6f4ea,stroke:#2f855a
    classDef unbounded fill:#fef5e7,stroke:#b7791f
    class B1,B2,B3,B4,B5 bounded
    class O1,O2,O3,O4,O5 unbounded
```

The left path is bounded, costs roughly one Haiku call, and returns a typed
`HealthReport`. The right path is an unbounded fan-out over many Sonnet and Haiku
calls, and returns free text that downstream code parses.

## Human-in-the-loop approval

Write tools exist only on the change-executor subagent. The main agent holds no
write tools at all, so a mutation cannot bypass approval. Every one of the 11
write tools is listed in `interrupt_on`, which pauses the graph before the call
executes.

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant CE as change-executor
    participant CP as MemorySaver
    participant S as Slack
    participant K as Kubernetes

    U->>O: "scale api to 5 replicas"
    O->>CE: task(agent="change-executor")
    CE->>K: read current state
    K-->>CE: 2 replicas
    Note over CE: about to call a write tool
    CE-->>CP: interrupt, graph state saved
    CP-->>O: __interrupt__
    O-->>S: send_hitl_request, Approve / Reject buttons

    alt Approved
        U->>S: click Approve
        S->>O: sre_approve, session_id
        O->>CP: Command(resume={"decisions":[{"type":"approve"}]})
        CP->>CE: continue
        CE->>K: kubectl_scale_deployment
        K-->>CE: scaled
        CE->>K: verify new state
        CE-->>O: before / after report
        O-->>S: update_hitl_resolved
    else Rejected
        U->>S: click Reject
        S->>O: sre_reject, session_id
        O->>CP: Command(resume={"decisions":[{"type":"reject"}]})
        CE-->>O: change not applied
        O-->>S: update_hitl_resolved
    end
```

The same interrupt and resume cycle is driven from three places, namely Slack
buttons (`sre_approve` / `sre_reject`), REST endpoints (`/api/approve`,
`/api/reject`, `/api/edit`), and the CLI loop in `main.py`. All of them resume
the same langgraph thread by `thread_id`.

## Cost and loop safety

Controls added after a runaway filesystem loop, all configured in `config.py`
and applied in `agent.py::_build_middleware`.

```mermaid
flowchart LR
    subgraph limits["Per-run guards"]
        R["RECURSION_LIMIT = 60<br/>langgraph super-step cap<br/>applied at every invoke"]
        M["MODEL_CALL_RUN_LIMIT = 40<br/>MODEL_CALL_THREAD_LIMIT = 120<br/>exit_behavior: end"]
        T["TOOL_CALL_RUN_LIMIT = 80<br/>exit_behavior: end"]
        F["FS_TOOL_RUN_LIMIT = 25<br/>grep, read_file, ls, glob<br/>exit_behavior: continue"]
    end

    subgraph cost["Cost reduction"]
        PC["Anthropic prompt caching<br/>caches system prompt,<br/>tool defs, history"]
        HK["Haiku for the 8 read-only<br/>subagents and the<br/>bounded health check"]
        ZT["Zero-token data collection<br/>in scheduler.py"]
    end
```

`exit_behavior` differs on purpose. Hitting the global caps ends the run, while
hitting the per-tool filesystem cap only blocks that tool, so the agent can
still summarize what it already found.

## Notes on the current wiring

Two things are true of the code as drawn and are worth knowing when reading it.

- `tools/__init__.py` exports `WRITE_TOOLS` (18 tools) and `WRITE_TOOL_NAMES`,
  but nothing imports either. The change-executor imports its 11 write tools
  directly, so 7 exported write tools are not reachable by any agent. Those are
  `kubectl_patch_configmap`, `kubectl_rollback_deployment`,
  `kubectl_apply_custom_resource`, `kubectl_delete_resource`,
  `helm_upgrade_release`, `helm_rollback_release`, `helm_add_repo`.
- The orchestrator path and the bounded path produce different output
  contracts. The bounded path returns a typed `HealthReport`, while the
  orchestrator returns free text with `[CRITICAL]` markers that downstream code
  parses with regex and substring matching.
