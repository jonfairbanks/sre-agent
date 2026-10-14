"""CLI entry point for the autonomous SRE bot."""
import os
import sys
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.markdown import Markdown
from langgraph.types import Command
from response_text import response_text

load_dotenv()

console = Console()


def check_env():
    try:
        from llm import validate_provider_credentials
        validate_provider_credentials()
    except (RuntimeError, ValueError) as error:
        console.print(f"[red]{error}[/red]")
        console.print("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)
    missing = []
    if not os.getenv("LANGSMITH_API_KEY"):
        missing.append("LANGSMITH_API_KEY")
    if missing:
        console.print(f"[red]Missing required env vars: {', '.join(missing)}[/red]")
        console.print("Copy .env.example to .env and fill in your keys.")
        sys.exit(1)


def handle_interrupt(action: dict) -> dict | None:
    """Review one tool call and return its decision, or None to cancel."""
    console.print()
    console.print(Panel(
        str(action),
        title="[yellow bold]Approval Required[/yellow bold]",
        border_style="yellow",
    ))
    console.print("[dim]y=approve  n=reject  e=edit  q=quit[/dim]")
    choice = Prompt.ask("Your decision", choices=["y", "n", "e", "q"], default="n")
    if choice == "y":
        return {"type": "approve"}
    if choice == "n":
        return {
            "type": "reject",
            "message": Prompt.ask("Reason for rejection (optional)", default=""),
        }
    if choice == "e":
        console.print("[dim]Enter changed arguments as key=value pairs. Empty line to finish.[/dim]")
        edited_args = dict(action.get("args", {}))
        while True:
            line = input("  ").strip()
            if not line:
                break
            if "=" in line:
                key, value = line.split("=", 1)
                edited_args[key.strip()] = value.strip()
        return {
            "type": "edit",
            "edited_action": {"name": action["name"], "args": edited_args},
        }
    return None


def run_with_hitl(agent, messages: list[dict], config: dict) -> dict:
    """Review every pending action and route decisions to its interrupt."""
    result = agent.invoke({"messages": messages}, config=config)
    while result.get("__interrupt__"):
        interrupts = result["__interrupt__"]
        resumes = {}
        for interrupt in interrupts:
            value = interrupt.value
            actions = value.get("action_requests", []) if isinstance(value, dict) else []
            if not actions:
                console.print("[yellow]Cannot review an interrupt without action requests.[/yellow]")
                return result
            decisions = []
            for action in actions:
                decision = handle_interrupt(action)
                if decision is None:
                    console.print("[yellow]Cancelled.[/yellow]")
                    return result
                decisions.append(decision)
            interrupt_id = getattr(interrupt, "id", None)
            if not interrupt_id and len(interrupts) > 1:
                console.print("[yellow]Cannot route multiple interrupts without IDs.[/yellow]")
                return result
            resumes[interrupt_id] = {"decisions": decisions}
        # Current LangGraph routes by ID; older single interrupts accept a payload.
        resume = resumes if None not in resumes else resumes[None]
        result = agent.invoke(Command(resume=resume), config=config)
    return result


def print_response(result: dict):
    """Print the final agent response."""
    messages = result.get("messages", [])
    if not messages:
        return
    content = response_text(messages[-1])
    if content:
        console.print()
        console.print(Panel(Markdown(content), title="[green bold]SRE Bot[/green bold]", border_style="green"))


def print_todos(result: dict):
    """Print todo list if present."""
    todos = result.get("todos", [])
    if not todos:
        return
    console.print("\n[dim]Task progress:[/dim]")
    icons = {"completed": "✅", "in_progress": "🔄", "pending": "⏳"}
    for todo in todos:
        icon = icons.get(todo.get("status", "pending"), "•")
        console.print(f"  {icon} {todo.get('content', '')}")


def main():
    check_env()

    console.print(Panel(
        "[bold green]Autonomous SRE Bot[/bold green]\n"
        "[dim]Kubernetes cluster monitoring, diagnosis, and remediation[/dim]\n\n"
        "Commands:\n"
        "  [bold]audit[/bold] — full cluster health check\n"
        "  [bold]pods <namespace>[/bold] — inspect pod health\n"
        "  [bold]scaling <namespace>[/bold] — analyze scaling & HPA\n"
        "  [bold]performance <namespace>[/bold] — CPU/memory analysis\n"
        "  [bold]logs <namespace>[/bold] — scan logs for errors\n"
        "  [bold]fix <description>[/bold] — apply a specific change\n"
        "  [bold]quit[/bold] — exit",
        title="SRE Bot",
        border_style="blue",
    ))

    # Import here so env vars are loaded first
    from agent import create_sre_agent
    from config import make_agent_config

    agent = create_sre_agent()
    config = make_agent_config("sre-main")
    session_messages = []

    console.print(f"\n[dim]LangSmith project: {os.getenv('LANGSMITH_PROJECT', 'default')}[/dim]")
    console.print("[dim]Type your request or 'quit' to exit.[/dim]\n")

    while True:
        try:
            user_input = Prompt.ask("[bold blue]You[/bold blue]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        # Expand shorthand commands
        if user_input.lower() == "audit":
            user_input = (
                "Run a full Kubernetes cluster health audit. Check all namespaces. "
                "Inspect pod health, scaling configuration, resource performance, and logs. "
                "Give me a prioritized list of issues and recommendations."
            )

        session_messages = [{"role": "user", "content": user_input}]

        with console.status("[bold green]Analyzing cluster...[/bold green]", spinner="dots"):
            pass  # Status shown during invoke below

        console.print(f"\n[dim]Running: {user_input[:80]}{'...' if len(user_input) > 80 else ''}[/dim]")

        try:
            result = run_with_hitl(agent, session_messages, config)
            print_todos(result)
            print_response(result)
        except Exception as e:
            console.print(f"\n[red]Error: {e}[/red]")
            import traceback
            console.print(f"[dim]{traceback.format_exc()}[/dim]")


if __name__ == "__main__":
    main()
