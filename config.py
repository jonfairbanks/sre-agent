import os
from dotenv import load_dotenv

load_dotenv()

MODEL = "claude-sonnet-4-6"
SUBAGENT_MODEL = "claude-haiku-4-5-20251001"  # Used for read-only subagents to reduce cost
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")

# In-cluster detection: Kubernetes injects SERVICE_ACCOUNT_TOKEN at this path
IN_CLUSTER = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

# For local dev: optionally pin to a specific context / namespace list
K8S_CONTEXT = os.getenv("K8S_CONTEXT", "")
DEFAULT_NAMESPACES = [
    ns.strip() for ns in os.getenv("DEFAULT_NAMESPACES", "").split(",") if ns.strip()
]

API_PORT = int(os.getenv("API_PORT", "8080"))

# ---------------------------------------------------------------------------
# Cost / runaway-loop safety limits
# ---------------------------------------------------------------------------
# A langgraph "super-step" cap. This is the hard backstop that guarantees the
# graph cannot loop forever (e.g. the agent grep/read_file-ing files in a
# cycle). Applied to every agent.invoke via make_agent_config().
RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "60"))

# Max model (LLM) calls. run_limit = per single invocation; thread_limit =
# cumulative across a conversation thread. Backstop against runaway token spend.
MODEL_CALL_RUN_LIMIT = int(os.getenv("MODEL_CALL_RUN_LIMIT", "40"))
MODEL_CALL_THREAD_LIMIT = int(os.getenv("MODEL_CALL_THREAD_LIMIT", "120"))

# Max total tool calls in a single run, and a tighter cap on the read-heavy
# filesystem tools that caused the original grep/read_file cost incident.
TOOL_CALL_RUN_LIMIT = int(os.getenv("TOOL_CALL_RUN_LIMIT", "80"))
FS_TOOL_RUN_LIMIT = int(os.getenv("FS_TOOL_RUN_LIMIT", "25"))

# Anthropic prompt caching — caches the large static system prompt + tool
# definitions + growing message history so a multi-step loop is not re-billed
# full input tokens on every model call.
PROMPT_CACHING = os.getenv("PROMPT_CACHING", "true").lower() in ("1", "true", "yes")


def make_agent_config(thread_id: str) -> dict:
    """Build the langgraph invoke config for an agent run.

    Centralises the recursion_limit so every invoke / HITL resume across the
    API, Slack, scheduler, and CLI enforces the same hard loop cap.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
