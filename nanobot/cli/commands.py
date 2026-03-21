"""CLI commands for nanobot."""

import asyncio
import os
import select
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

# Force UTF-8 encoding for Windows console
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # Re-open stdout/stderr with UTF-8 encoding
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__
from nanobot.cli.doctor import DoctorReport, resolve_config_path, run_doctor
from nanobot.config.paths import get_workspace_path
from nanobot.config.schema import Config
from nanobot.feishu.broadcast import FeishuBroadcastService
from nanobot.feishu.client import FeishuClient
from nanobot.feishu.outbound import FeishuOutboundMessenger
from nanobot.utils.helpers import sync_workspace_templates
from nanobot.version import format_version

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}
_UPSTREAM_CORE_PATHS = ("nanobot/feishu", "nanobot/agent", "nanobot/config", "deploy", "docs")
_DOCTOR_STATUS_LABELS = {
    "ok": "[green]OK[/green]",
    "warn": "[yellow]WARN[/yellow]",
    "error": "[red]ERROR[/red]",
    "fixed": "[cyan]FIXED[/cyan]",
    "skipped": "[dim]SKIPPED[/dim]",
}

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # original termios settings, restored on exit


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    try:
        import termios
        termios.tcflush(fd, termios.TCIFLUSH)
        return
    except Exception:
        pass

    try:
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break
    except Exception:
        return


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    from nanobot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # Enter submits (single line mode)
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """Render assistant response with consistent terminal styling."""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()


def _git_capture(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the completed process with captured text output."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
    )


def _top_level_paths(paths: list[str]) -> list[str]:
    tops = {path.split("/", 1)[0] for path in paths if path}
    return sorted(tops)


def _classify_upstream_risk(commit_paths: list[str], local_paths: set[str]) -> str:
    commit_top = set(_top_level_paths(commit_paths))
    if commit_top & local_paths:
        return "high"
    if any(path.startswith(_UPSTREAM_CORE_PATHS) for path in commit_paths):
        return "medium"
    if commit_top & set(_UPSTREAM_CORE_PATHS):
        return "medium"
    return "low"
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc



def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} {format_version()}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nanobot configuration and workspace."""
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import Config

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        console.print("  [bold]y[/bold] = overwrite with defaults (existing values will be lost)")
        console.print("  [bold]N[/bold] = refresh config, keeping existing values and adding new fields")
        if typer.confirm("Overwrite?"):
            config = Config()
            save_config(config)
            console.print(f"[green]✓[/green] Config reset to defaults at {config_path}")
        else:
            config = load_config()
            save_config(config)
            console.print(f"[green]✓[/green] Config refreshed at {config_path} (existing values preserved)")
    else:
        save_config(Config())
        console.print(f"[green]✓[/green] Created config at {config_path}")

    # Create workspace
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]nanobot agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")





def _make_provider(config: Config):
    """Create the appropriate LLM provider from config."""
    from nanobot.providers.azure_openai_provider import AzureOpenAIProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # Custom: direct OpenAI-compatible endpoint, bypasses LiteLLM
    from nanobot.providers.custom_provider import CustomProvider
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    # Azure OpenAI: direct Azure OpenAI endpoint with deployment name
    if provider_name == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            console.print("[red]Error: Azure OpenAI requires api_key and api_base.[/red]")
            console.print("Set them in ~/.nanobot/config.json under providers.azure_openai section")
            console.print("Use the model field to specify the deployment name.")
            raise typer.Exit(1)

        return AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )

    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.registry import find_by_name
    spec = find_by_name(provider_name) if provider_name else None
    if not model.startswith("bedrock/") and not (p and p.api_key) and not (spec and spec.is_oauth):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)

    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
        provider_name=provider_name,
    )


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from nanobot.config.loader import load_config, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]Error: Config file not found: {config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]Using config: {config_path}[/dim]")

    loaded = load_config(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _load_persisted_config(config: str | None = None) -> tuple[Path, Config]:
    """Load the exact config file targeted by an operator command."""
    from nanobot.config.loader import load_config, set_config_path

    path = resolve_config_path(config)
    if config:
        set_config_path(path)
    if not path.exists():
        console.print(f"[red]Error: Config file not found: {path}[/red]")
        raise typer.Exit(1)
    return path, load_config(path)


def _print_restart_note() -> None:
    console.print("[yellow]Restart required to apply config changes.[/yellow]")


def _print_doctor_report(report: DoctorReport) -> None:
    console.print(f"{__logo__} nanobot Doctor\n")
    console.print(Text(f"Config: {report.config_path}", no_wrap=True, overflow="ignore"))
    if report.workspace_path is not None:
        console.print(Text(f"Workspace: {report.workspace_path}", no_wrap=True, overflow="ignore"))
    console.print()

    table = Table(title="Doctor Findings")
    table.add_column("Status", style="cyan", no_wrap=True)
    table.add_column("Check", style="magenta", no_wrap=True)
    table.add_column("Summary", style="white")

    for finding in report.findings:
        summary = finding.summary
        if finding.detail:
            summary = f"{summary} [{finding.detail}]"
        table.add_row(_DOCTOR_STATUS_LABELS[finding.status], finding.key, summary)

    console.print(table)
    if report.restart_required:
        console.print()
        _print_restart_note()


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="Gateway port"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Start the nanobot gateway."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.feishu.persona import FeishuUserWorkspaceManager
    from nanobot.heartbeat.service import HeartbeatService, HeartbeatTarget
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    runtime_config = _load_runtime_config(config_path, workspace)
    port = port if port is not None else runtime_config.gateway.port

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(runtime_config)
    session_manager = SessionManager(runtime_config.workspace_path)
    feishu_workspace_manager = FeishuUserWorkspaceManager(runtime_config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=runtime_config.workspace_path,
        model=runtime_config.agents.defaults.model,
        temperature=runtime_config.agents.defaults.temperature,
        max_tokens=runtime_config.agents.defaults.max_tokens,
        max_iterations=runtime_config.agents.defaults.max_tool_iterations,
        memory_window=runtime_config.agents.defaults.memory_window,
        reasoning_effort=runtime_config.agents.defaults.reasoning_effort,
        brave_api_key=runtime_config.tools.web.search.api_key or None,
        web_proxy=runtime_config.tools.web.proxy or None,
        exec_config=runtime_config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=runtime_config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=runtime_config.tools.mcp_servers,
        channels_config=runtime_config.channels,
    )

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool
        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        # Prevent the agent from scheduling new cron jobs during execution
        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response
            ))
        return response
    cron.on_job = on_cron_job

    # Create channel manager
    channels = ChannelManager(
        runtime_config,
        bus,
        session_manager=session_manager,
        provider=provider,
        model=agent.model,
        memory_window=agent.memory_window,
    )

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        # Prefer the most recently updated non-internal session on an enabled channel.
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # Fallback keeps prior behavior but remains explicit.
        return "cli", "direct"

    # Create heartbeat service
    async def on_heartbeat_execute(target: HeartbeatTarget, tasks: str):
        """Phase 2: execute heartbeat tasks through the full agent loop."""
        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_heartbeat_direct(
            tasks,
            channel=target.channel,
            chat_id=target.chat_id,
            workspace_root=target.workspace_root,
            on_progress=_silent,
            overlay_context=target.overlay_context,
        )

    async def on_heartbeat_notify(target: HeartbeatTarget, response: str) -> None:
        """Deliver a heartbeat response to the user's channel."""
        from nanobot.bus.events import OutboundMessage
        if target.channel == "cli":
            return  # No external channel available to deliver to
        await bus.publish_outbound(OutboundMessage(channel=target.channel, chat_id=target.chat_id, content=response))

    def _fallback_heartbeat_target() -> HeartbeatTarget:
        channel, chat_id = _pick_heartbeat_target()
        return HeartbeatTarget(
            workspace_root=runtime_config.workspace_path,
            channel=channel,
            chat_id=chat_id,
            session_key="heartbeat",
            overlay_context=None,
        )

    hb_cfg = runtime_config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=runtime_config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
        target_provider=feishu_workspace_manager.list_heartbeat_targets,
        fallback_target_provider=_fallback_heartbeat_target,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        except Exception:
            import traceback
            console.print("\n[red]Error: Gateway crashed unexpectedly[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

    asyncio.run(run())


# ============================================================================
# Heartbeat Commands
# ============================================================================

upstream_app = typer.Typer(help="Inspect upstream git commits")
app.add_typer(upstream_app, name="upstream")


@upstream_app.command("status")
def upstream_status(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Git workspace to inspect"),
):
    """Show upstream-only commits and a git-based import risk hint."""
    repo = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()

    try:
        _git_capture(["rev-parse", "--is-inside-work-tree"], repo)
    except subprocess.CalledProcessError:
        console.print(f"[red]Not a git repository:[/red] {repo}")
        raise typer.Exit(1)

    remotes = _git_capture(["remote"], repo).stdout.split()
    if "upstream" not in remotes:
        console.print("[red]Missing git remote 'upstream'.[/red]")
        raise typer.Exit(1)

    try:
        _git_capture(["fetch", "--no-tags", "upstream", "main"], repo)
    except subprocess.CalledProcessError as exc:
        console.print(f"[red]Failed to fetch upstream/main:[/red] {exc.stderr.strip() or exc.stdout.strip()}")
        raise typer.Exit(1)

    merge_base = _git_capture(["merge-base", "HEAD", "upstream/main"], repo).stdout.strip()
    local_changed = _git_capture(["diff", "--name-only", f"{merge_base}..HEAD"], repo).stdout.splitlines()
    local_top_paths = set(_top_level_paths(local_changed))
    upstream_only = _git_capture(["rev-list", "--reverse", "HEAD..upstream/main"], repo).stdout.splitlines()

    console.print(f"{__logo__} Upstream Status\n")
    console.print(Text(f"Repo: {repo}", no_wrap=True, overflow="ignore"))
    console.print("Base: upstream/main")
    console.print(f"Upstream-only commits: {len(upstream_only)}")

    if not upstream_only:
        console.print("[green]Already up to date with upstream/main.[/green]")
        return

    for sha in upstream_only:
        header = _git_capture(["show", "--quiet", "--date=short", "--format=%h%x09%ad%x09%s", sha], repo).stdout.strip()
        changed_files = [line.strip() for line in _git_capture(["show", "--name-only", "--format=", sha], repo).stdout.splitlines() if line.strip()]
        top_paths = _top_level_paths(changed_files)
        risk = _classify_upstream_risk(changed_files, local_top_paths)

        short_sha, date, subject = (header.split("\t", 2) + ["", "", ""])[:3]
        console.print(f"\n[{risk.upper()}] {short_sha}  {date}  {subject}")
        console.print(f"Paths: {', '.join(top_paths) if top_paths else '(none)'}")
        console.print(f"Next: git show {short_sha}")
        console.print(f"      git cherry-pick -x {short_sha}")


heartbeat_app = typer.Typer(help="Manage heartbeat configuration")
app.add_typer(heartbeat_app, name="heartbeat")


@heartbeat_app.command("status")
def heartbeat_status(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Show the configured heartbeat state."""
    path, runtime_config = _load_persisted_config(config)
    heartbeat = runtime_config.gateway.heartbeat

    console.print(f"{__logo__} Heartbeat Status\n")
    console.print(Text(f"Config: {path}", no_wrap=True, overflow="ignore"))
    console.print(f"Enabled: {'yes' if heartbeat.enabled else 'no'}")
    console.print(f"Interval: {heartbeat.interval_s}s")


@heartbeat_app.command("on")
def heartbeat_on(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Persistently enable heartbeat in config."""
    from nanobot.config.loader import save_config

    path, runtime_config = _load_persisted_config(config)
    changed = not runtime_config.gateway.heartbeat.enabled
    runtime_config.gateway.heartbeat.enabled = True
    if changed:
        save_config(runtime_config, path)
        console.print(f"[green]✓[/green] Enabled heartbeat in {path}")
        _print_restart_note()
        return

    console.print(f"[green]Heartbeat is already enabled.[/green] Config: {path}")


@heartbeat_app.command("off")
def heartbeat_off(
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Persistently disable heartbeat in config."""
    from nanobot.config.loader import save_config

    path, runtime_config = _load_persisted_config(config)
    changed = runtime_config.gateway.heartbeat.enabled
    runtime_config.gateway.heartbeat.enabled = False
    if changed:
        save_config(runtime_config, path)
        console.print(f"[green]✓[/green] Disabled heartbeat in {path}")
        _print_restart_note()
        return

    console.print(f"[green]Heartbeat is already disabled.[/green] Config: {path}")


@heartbeat_app.command("set-interval")
def heartbeat_set_interval(
    seconds: int = typer.Argument(..., help="Heartbeat interval in seconds"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Persistently set the heartbeat interval in seconds."""
    from nanobot.config.loader import save_config

    if seconds <= 0:
        console.print("[red]Heartbeat interval must be a positive integer.[/red]")
        raise typer.Exit(1)

    path, runtime_config = _load_persisted_config(config)
    previous = runtime_config.gateway.heartbeat.interval_s
    runtime_config.gateway.heartbeat.interval_s = seconds
    if previous != seconds:
        save_config(runtime_config, path)
        console.print(f"[green]✓[/green] Updated heartbeat interval: {previous}s -> {seconds}s")
        console.print(f"Config: {path}")
        _print_restart_note()
        return

    console.print(f"[green]Heartbeat interval is already {seconds}s.[/green] Config: {path}")


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="Session ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="Workspace directory"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="Config file path"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="Render assistant output as Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="Show nanobot runtime logs during chat"),
):
    """Interact with the agent directly."""
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService

    runtime_config = _load_runtime_config(config_path, workspace)
    sync_workspace_templates(runtime_config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(runtime_config)

    # Create cron service for tool usage (no callback needed for CLI unless running)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=runtime_config.workspace_path,
        model=runtime_config.agents.defaults.model,
        temperature=runtime_config.agents.defaults.temperature,
        max_tokens=runtime_config.agents.defaults.max_tokens,
        max_iterations=runtime_config.agents.defaults.max_tool_iterations,
        memory_window=runtime_config.agents.defaults.memory_window,
        reasoning_effort=runtime_config.agents.defaults.reasoning_effort,
        brave_api_key=runtime_config.tools.web.search.api_key or None,
        web_proxy=runtime_config.tools.web.proxy or None,
        exec_config=runtime_config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=runtime_config.tools.restrict_to_workspace,
        mcp_servers=runtime_config.tools.mcp_servers,
        channels_config=runtime_config.channels,
    )

    # Show spinner when logs are off (no output to miss); skip when logs are on
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # Animated spinner is safe to use with prompt_toolkit input handling
        return console.status("[dim]nanobot is thinking...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from nanobot.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} Interactive mode (type [bold]exit[/bold] or [bold]Ctrl+C[/bold] to quit)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\nReceived {sig_name}, goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[str] = []

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
                        if msg.metadata.get("_progress"):
                            is_tool_hint = msg.metadata.get("_tool_hint", False)
                            ch = agent_loop.channels_config
                            if ch and is_tool_hint and not ch.send_tool_hints:
                                pass
                            elif ch and not is_tool_hint and not ch.send_progress:
                                pass
                            else:
                                console.print(f"  [dim]↳ {msg.content}[/dim]")
                        elif not turn_done.is_set():
                            if msg.content:
                                turn_response.append(msg.content)
                            turn_done.set()
                        elif msg.content:
                            console.print()
                            _print_agent_response(msg.content, render_markdown=markdown)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        user_input = await _read_interactive_input_async()
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\nGoodbye!")
                            break

                        turn_done.clear()
                        turn_response.clear()

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                        ))

                        with _thinking_ctx():
                            await turn_done.wait()

                        if turn_response:
                            _print_agent_response(turn_response[0], render_markdown=markdown)
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\nGoodbye!")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")

feishu_app = typer.Typer(help="Manage Feishu operations")
app.add_typer(feishu_app, name="feishu")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        wa.bridge_url
    )

    dc = config.channels.discord
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc.gateway_url
    )

    # Feishu
    fs = config.channels.feishu
    fs_config = f"app_id: {fs.app_id[:10]}..." if fs.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "Feishu",
        "✓" if fs.enabled else "✗",
        fs_config
    )

    # Mochat
    mc = config.channels.mochat
    mc_base = mc.base_url or "[dim]not configured[/dim]"
    table.add_row(
        "Mochat",
        "✓" if mc.enabled else "✗",
        mc_base
    )

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    # Slack
    slack = config.channels.slack
    slack_config = "socket" if slack.app_token and slack.bot_token else "[dim]not configured[/dim]"
    table.add_row(
        "Slack",
        "✓" if slack.enabled else "✗",
        slack_config
    )

    # DingTalk
    dt = config.channels.dingtalk
    dt_config = f"client_id: {dt.client_id[:10]}..." if dt.client_id else "[dim]not configured[/dim]"
    table.add_row(
        "DingTalk",
        "✓" if dt.enabled else "✗",
        dt_config
    )

    # QQ
    qq = config.channels.qq
    qq_config = f"app_id: {qq.app_id[:10]}..." if qq.app_id else "[dim]not configured[/dim]"
    table.add_row(
        "QQ",
        "✓" if qq.enabled else "✗",
        qq_config
    )

    # Email
    em = config.channels.email
    em_config = em.imap_host if em.imap_host else "[dim]not configured[/dim]"
    table.add_row(
        "Email",
        "✓" if em.enabled else "✗",
        em_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    from nanobot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    from nanobot.config.loader import load_config
    from nanobot.config.paths import get_runtime_subdir

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token
    env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


def _resolve_broadcast_message(message: str | None, message_file: str | None) -> str:
    if bool(message) == bool(message_file):
        console.print("[red]Specify exactly one of --message or --message-file.[/red]")
        raise typer.Exit(1)

    if message is not None:
        content = message.strip()
    else:
        assert message_file is not None
        path = Path(message_file).expanduser().resolve()
        if not path.exists():
            console.print(f"[red]Message file not found: {path}[/red]")
            raise typer.Exit(1)
        content = path.read_text(encoding="utf-8").strip()

    if not content:
        console.print("[red]Broadcast message cannot be empty.[/red]")
        raise typer.Exit(1)
    return content


@feishu_app.command("broadcast")
def feishu_broadcast(
    message: str | None = typer.Option(None, "--message", help="Broadcast message text"),
    message_file: str | None = typer.Option(None, "--message-file", help="UTF-8 file containing the broadcast message"),
    send: bool = typer.Option(False, "--send", help="Actually send the broadcast; default is dry-run"),
    confirm: str = typer.Option("", "--confirm", help="Type SEND to confirm a real broadcast"),
    page_size: int = typer.Option(100, "--page-size", min=1, max=100, help="Feishu contact page size"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Optional max recipients for testing"),
    throttle_seconds: float = typer.Option(0.2, "--throttle-seconds", min=0.0, help="Delay between sends"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Broadcast a one-off Feishu announcement to all active employees."""
    content = _resolve_broadcast_message(message, message_file)
    runtime_config = _load_runtime_config(config)
    feishu_config = runtime_config.channels.feishu

    if not feishu_config.enabled:
        console.print("[red]Feishu channel is not enabled in config.[/red]")
        raise typer.Exit(1)
    if not feishu_config.app_id or not feishu_config.app_secret:
        console.print("[red]Feishu appId/appSecret are required for broadcast.[/red]")
        raise typer.Exit(1)

    if send and confirm != "SEND":
        console.print("[red]Real broadcast requires --confirm SEND.[/red]")
        raise typer.Exit(1)

    client = FeishuClient.build(feishu_config)
    messenger = FeishuOutboundMessenger(lambda: client)
    service = FeishuBroadcastService(client=client, messenger=messenger)

    recipients = service.list_active_recipients(page_size=page_size, limit=limit)
    console.print(f"Found {len(recipients)} active users.")
    for recipient in recipients[:10]:
        console.print(f"- {recipient.name} ({recipient.open_id})")
    if len(recipients) > 10:
        console.print(f"[dim]... and {len(recipients) - 10} more[/dim]")

    if not recipients:
        console.print("[yellow]No active recipients found.[/yellow]")
        raise typer.Exit(0)

    if not send:
        console.print(f"[green]Dry run:[/green] would send to {len(recipients)} active users.")
        raise typer.Exit(0)

    result = asyncio.run(service.broadcast(content, recipients, throttle_seconds=throttle_seconds))
    console.print(
        f"[green]Broadcast finished.[/green] total={result.total} sent={len(result.succeeded)} failed={len(result.failed)}"
    )
    if result.failed:
        console.print("[yellow]Failed recipients:[/yellow]")
        for recipient in result.failed[:20]:
            console.print(f"- {recipient.name} ({recipient.open_id})")
        if len(result.failed) > 20:
            console.print(f"[dim]... and {len(result.failed) - 20} more failures[/dim]")
        raise typer.Exit(1)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """Show nanobot status."""
    from nanobot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Apply safe local fixes"),
    config: str | None = typer.Option(None, "--config", "-c", help="Path to config file"),
):
    """Inspect core nanobot health and optionally apply safe local fixes."""
    report = run_doctor(config, fix=fix)
    _print_doctor_report(report)
    if report.has_remaining_issues:
        raise typer.Exit(1)


# ============================================================================
# OAuth Login
# ============================================================================

provider_app = typer.Typer(help="Manage providers")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, Callable[..., object]] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth provider (e.g. 'openai-codex', 'github-copilot')"),
):
    """Authenticate with an OAuth provider."""
    from nanobot.providers.registry import PROVIDERS

    key = provider.replace("-", "_")
    spec = next((s for s in PROVIDERS if s.name == key and s.is_oauth), None)
    if not spec:
        names = ", ".join(s.name.replace("_", "-") for s in PROVIDERS if s.is_oauth)
        console.print(f"[red]Unknown OAuth provider: {provider}[/red]  Supported: {names}")
        raise typer.Exit(1)

    handler = _LOGIN_HANDLERS.get(spec.name)
    if not handler:
        console.print(f"[red]Login not implemented for {spec.label}[/red]")
        raise typer.Exit(1)

    console.print(f"{__logo__} OAuth Login - {spec.label}\n")
    handler()


@_register_login("openai_codex")
def _login_openai_codex() -> None:
    try:
        from oauth_cli_kit import get_token, login_oauth_interactive
        token = None
        try:
            token = get_token()
        except Exception:
            pass
        if not (token and token.access):
            console.print("[cyan]Starting interactive OAuth login...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ Authentication failed[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ Authenticated with OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]oauth_cli_kit not installed. Run: pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]Starting GitHub Copilot device flow...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ Authenticated with GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]Authentication error: {e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
