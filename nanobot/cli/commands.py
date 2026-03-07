"""nanobot 的 CLI 命令。"""

import asyncio
import os
import select
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from nanobot import __logo__, __version__
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}

# ---------------------------------------------------------------------------
# CLI 输入：使用 prompt_toolkit 处理编辑、粘贴、历史记录和显示
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None
_SAVED_TERM_ATTRS = None  # 原始 termios 设置，在退出时恢复


def _flush_pending_tty_input() -> None:
    """在模型生成输出时，丢弃由于用户输入产生的未读取键盘点击字节。"""
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
    """将终端恢复到其原始状态（例如回显、行缓冲等）。"""
    if _SAVED_TERM_ATTRS is None:
        return
    try:
        import termios
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)
    except Exception:
        pass


def _init_prompt_session() -> None:
    """创建带有持久化文件历史的 prompt_toolkit 会话。"""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # 保存终端状态，以便在退出时恢复
    try:
        import termios
        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())
    except Exception:
        pass

    history_file = Path.home() / ".nanobot" / "history" / "cli_history"
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=FileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,   # 回车提交（单行模式）
    )


def _print_agent_response(response: str, render_markdown: bool) -> None:
    """以一致的终端样式渲染助手的响应。"""
    content = response or ""
    body = Markdown(content) if render_markdown else Text(content)
    console.print()
    console.print(f"[cyan]{__logo__} nanobot[/cyan]")
    console.print(body)
    console.print()


def _is_exit_command(command: str) -> bool:
    """判断输入是否应结束交互式聊天。"""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """使用 prompt_toolkit 读取用户输入（处理粘贴、历史记录和界面显示）。

    prompt_toolkit 原生支持：
    - 多行粘贴（括号粘贴模式）
    - 历史导航（上下方向键）
    - 干净的显示（无幽灵字符或伪影）
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
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanobot - 个人 AI 助手。"""
    pass


# ============================================================================
# 初始化配置 (Onboard / Setup)
# ============================================================================


@app.command()
def onboard():
    """初始化 nanobot 的配置与工作区。"""
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.config.schema import Config
    from nanobot.utils.helpers import get_workspace_path

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

    # 创建工作区
    workspace = get_workspace_path()

    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] Created workspace at {workspace}")

    sync_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\n后续步骤：")
    console.print("  1. 在 [cyan]~/.nanobot/config.json[/cyan] 中添加您的 API 密钥")
    console.print("     获取地址：https://openrouter.ai/keys")
    console.print("  2. 开始聊天：[cyan]nanobot agent -m \"你好！\"[/cyan]")
    console.print("\n[dim]希望接入 Telegram/WhatsApp？请参阅：https://github.com/HKUDS/nanobot#-chat-apps[/dim]")





def _make_provider(config: Config):
    """根据配置创建对应的 LLM provider 实例。"""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)

    # OpenAI Codex (OAuth 认证)
    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    # 自定义：直接请求兼容 OpenAI 规范的端点，绕过 LiteLLM
    if provider_name == "custom":
        return CustomProvider(
            api_key=p.api_key if p else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    from nanobot.providers.registry import find_by_name
    spec = find_by_name(str(provider_name)) if provider_name else None
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


@dataclass
class _FeishuOAuthStack:
    store: Any
    oauth_service: Any
    token_manager: Any
    callback_service: Any


def _build_feishu_oauth_stack(config: Config) -> _FeishuOAuthStack | None:
    oauth_cfg = config.integrations.feishu.oauth
    if not oauth_cfg.enabled:
        return None

    auth = config.resolve_feishu_auth()
    api_base = config.resolve_feishu_api_base()

    if not auth.app_id or not auth.app_secret:
        console.print("[yellow]Warning: Feishu OAuth enabled but app_id/app_secret missing, OAuth callback disabled.[/yellow]")
        return None

    public_base_url = str(oauth_cfg.public_base_url or "").strip().rstrip("/")
    callback_path = str(oauth_cfg.callback_path or "/oauth/feishu/callback").strip() or "/oauth/feishu/callback"
    if not callback_path.startswith("/"):
        callback_path = f"/{callback_path}"
    if not public_base_url:
        console.print("[yellow]Warning: Feishu OAuth enabled but public_base_url missing, OAuth callback disabled.[/yellow]")
        return None

    redirect_uri = f"{public_base_url}{callback_path}"
    bind_host = str(oauth_cfg.bind_host or config.gateway.host or "0.0.0.0").strip() or "0.0.0.0"
    bind_port = int(oauth_cfg.bind_port or config.gateway.port)

    from nanobot.oauth import (
        FeishuOAuthClient,
        FeishuOAuthService,
        FeishuUserTokenManager,
        OAuthCallbackService,
    )
    from nanobot.storage import SQLiteStore

    store = SQLiteStore(config.workspace_path / "memory" / "feishu" / "state.sqlite3")
    client = FeishuOAuthClient(
        api_base=api_base,
        app_id=auth.app_id,
        app_secret=auth.app_secret,
    )
    oauth_service = FeishuOAuthService(
        store=store,
        client=client,
        redirect_uri=redirect_uri,
        scopes=list(oauth_cfg.scopes or []),
        state_ttl_seconds=int(oauth_cfg.state_ttl_seconds),
    )
    token_manager = FeishuUserTokenManager(
        store=store,
        client=client,
        refresh_ahead_seconds=int(oauth_cfg.refresh_ahead_seconds),
    )
    callback_service = OAuthCallbackService(
        host=bind_host,
        port=bind_port,
        callback_path=callback_path,
        feishu_service=oauth_service,
        success_title=str(oauth_cfg.success_html_title or "Feishu Authorization Completed"),
        failure_title=str(oauth_cfg.failure_html_title or "Feishu Authorization Failed"),
    )
    return _FeishuOAuthStack(
        store=store,
        oauth_service=oauth_service,
        token_manager=token_manager,
        callback_service=callback_service,
    )


# ============================================================================
# 网关 / 服务器 (Gateway / Server)
# ============================================================================


@app.command()
def gateway(
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
):
    """启动 nanobot 网关服务。"""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")

    config = load_config()
    config.gateway.port = int(port)
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)
    oauth_stack = _build_feishu_oauth_stack(config)
    if oauth_stack is not None:
        oauth_stack.store.cleanup_expired_oauth_states(now_iso=datetime.now().isoformat())

    # 首先创建 Cron 服务（将会在 agent 创建后设置回调）
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # 连同 Cron 服务一并创建 agent
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        llm_timeout_seconds=config.agents.defaults.llm_timeout_seconds,
        stage_heartbeat_seconds=config.agents.defaults.stage_heartbeat_seconds,
        skillspec_render_primary_timeout_seconds=config.agents.defaults.skillspec_render_primary_timeout_seconds,
        skillspec_render_retry_timeout_seconds=config.agents.defaults.skillspec_render_retry_timeout_seconds,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        feishu_data_config=config.tools.feishu_data,
        response_template_config=config.agents.response_templates,
        skillspec_config=config.agents.skillspec,
        skillspec_embedding_provider_config=config.providers.siliconflow,
        feishu_oauth_service=oauth_stack.oauth_service if oauth_stack else None,
    )

    # 设置 Cron 任务的回调（需要依赖 agent）
    async def on_cron_job(job: CronJob) -> str | None:
        """通过 agent 执行定时任务。"""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    cron.on_job = on_cron_job

    # 创建渠道管理器
    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        """为心跳触发的消息选择一个可路由的渠道/聊天目标。"""
        enabled = set(channels.enabled_channels)
        # 倾向于在已启用的渠道上下文中，选择最近更新的非内部会话。
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        # 兜底策略保留了先前的行为，同时保持显式指定。
        return "cli", "direct"

    # 创建心跳服务
    async def on_heartbeat_execute(tasks: str) -> str:
        """阶段 2：通过完整的 agent 循环执行心跳任务。"""
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        """将心跳响应传递至用户的渠道。"""
        from nanobot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return  # 没有可用的外部渠道进行投递
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")
    if oauth_stack is not None:
        oauth_cfg = config.integrations.feishu.oauth
        callback_path = str(oauth_cfg.callback_path or "/oauth/feishu/callback")
        if not callback_path.startswith("/"):
            callback_path = f"/{callback_path}"
        bind_host = str(oauth_cfg.bind_host or config.gateway.host or "0.0.0.0")
        bind_port = int(oauth_cfg.bind_port or config.gateway.port)
        public_base_url = str(oauth_cfg.public_base_url or "").strip().rstrip("/")
        console.print(f"[green]✓[/green] Feishu OAuth callback ingress: {bind_host}:{bind_port}{callback_path}")
        if public_base_url:
            console.print(f"[green]✓[/green] Feishu OAuth redirect_uri: {public_base_url}{callback_path}")

    async def run():
        try:
            if oauth_stack is not None:
                oauth_stack.callback_service.start()
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
        finally:
            await agent.close_mcp()
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            if oauth_stack is not None:
                oauth_stack.callback_service.stop()
                oauth_stack.store.close()

    asyncio.run(run())




# ============================================================================
# Agent 交互命令 (Agent Commands)
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="发送给 agent 的消息"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="会话 ID"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="将助手的输出渲染为 Markdown"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="在聊天期间显示 nanobot 的运行日志"),
):
    """直接与 agent 智能体进行交互。"""
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService

    config = load_config()
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)

    # 为工具使用创建 Cron 服务（除非正在运行否则 CLI 不需要回调）
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("nanobot")
    else:
        logger.disable("nanobot")

    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        llm_timeout_seconds=config.agents.defaults.llm_timeout_seconds,
        stage_heartbeat_seconds=config.agents.defaults.stage_heartbeat_seconds,
        skillspec_render_primary_timeout_seconds=config.agents.defaults.skillspec_render_primary_timeout_seconds,
        skillspec_render_retry_timeout_seconds=config.agents.defaults.skillspec_render_retry_timeout_seconds,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        feishu_data_config=config.tools.feishu_data,
        response_template_config=config.agents.response_templates,
        skillspec_config=config.agents.skillspec,
        skillspec_embedding_provider_config=config.providers.siliconflow,
    )

    # 当日志关闭时显示加载动画（避免错过输出）；日志开启时则跳过
    def _thinking_ctx():
        if logs:
            from contextlib import nullcontext
            return nullcontext()
        # 动画加载器可以安全地与 prompt_toolkit 的输入处理一起使用
        return console.status("[dim]nanobot 正在思考...[/dim]", spinner="dots")

    async def _cli_progress(content: str, *, tool_hint: bool = False) -> None:
        ch = agent_loop.channels_config
        if ch and tool_hint and not ch.send_tool_hints:
            return
        if ch and not tool_hint and not ch.send_progress:
            return
        console.print(f"  [dim]↳ {content}[/dim]")

    if message:
        # 单条消息模式 — 直接调用，不需要总线 (bus)
        async def run_once():
            with _thinking_ctx():
                response = await agent_loop.process_direct(message, session_id, on_progress=_cli_progress)
            _print_agent_response(response, render_markdown=markdown)
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # 交互模式 — 像其他渠道一样通过总线 (bus) 路由
        from nanobot.bus.events import InboundMessage
        _init_prompt_session()
        console.print(f"{__logo__} 交互模式 (输入 [bold]exit[/bold] 或按 [bold]Ctrl+C[/bold] 退出)\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _exit_on_sigint(signum, frame):
            _restore_terminal()
            console.print("\nGoodbye!")
            os._exit(0)

        signal.signal(signal.SIGINT, _exit_on_sigint)

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
# 渠道命令 (Channel Commands)
# ============================================================================


channels_app = typer.Typer(help="管理消息渠道")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """显示各消息渠道的状态。"""
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="渠道状态 (Channel Status)")
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
    """获取 bridge 目录，并在需要时进行安装设置。"""
    import shutil
    import subprocess

    # 用户的 bridge 存放位置
    user_bridge = Path.home() / ".nanobot" / "bridge"

    # 检查是否已经构建过
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # 检查环境中是否包含 npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # 查找 source bridge：先检查打包安装的数据，然后检查开发源码目录
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

    # 复制到用户目录
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # 安装依赖包并打包构建
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
    """通过扫描二维码链接设备。"""
    import subprocess

    from nanobot.config.loader import load_config

    config = load_config()
    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} 正在启动 bridge 服务...")
    console.print("请扫描二维码进行连接。\n")

    env = {**os.environ}
    if config.channels.whatsapp.bridge_token:
        env["BRIDGE_TOKEN"] = config.channels.whatsapp.bridge_token

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# ============================================================================
# Cron 定时任务命令 (Cron Commands)
# ============================================================================

cron_app = typer.Typer(help="管理定时调度任务")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="包含已禁用的任务"),
):
    """列出所有已调度的定时任务。"""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("无已调度的定时任务。")
        return

    table = Table(title="已调度的任务 (Scheduled Jobs)")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    import time
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    for job in jobs:
        # 格式化调度时间表达式
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = f"{job.schedule.expr or ''} ({job.schedule.tz})" if job.schedule.tz else (job.schedule.expr or "")
        else:
            sched = "one-time"

        # 格式化下次运行时间
        next_run = ""
        if job.state.next_run_at_ms:
            ts = job.state.next_run_at_ms / 1000
            try:
                tz = ZoneInfo(job.schedule.tz) if job.schedule.tz else None
                next_run = _dt.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_run = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="任务名称"),
    message: str = typer.Option(..., "--message", "-m", help="发给 agent 的消息内容"),
    every: int = typer.Option(None, "--every", "-e", help="每隔 N 秒执行一次"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron 表达式 (例如 '0 9 * * *')"),
    tz: str | None = typer.Option(None, "--tz", help="Cron 表达式对应的 IANA 时区 (例如 'America/Vancouver')"),
    at: str = typer.Option(None, "--at", help="在指定时间单次执行 (ISO 时间格式)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="将执行响应投递至指定渠道"),
    to: str = typer.Option(None, "--to", help="投递响应时的接收方 ID"),
    channel: str = typer.Option(None, "--channel", help="响应投递的目标渠道 (例如 'telegram', 'whatsapp')"),
):
    """添加一个新的定时调度任务。"""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

    if tz and not cron_expr:
        console.print("[red]Error: --tz can only be used with --cron[/red]")
        raise typer.Exit(1)

    # 确定任务调度的类型
    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
    elif at:
        import datetime
        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    try:
        job = service.add_job(
            name=name,
            schedule=schedule,
            message=message,
            deliver=deliver,
            to=to,
            channel=channel,
        )
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="需要被删除的任务 ID"),
):
    """删除指定的定时任务。"""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="任务 ID"),
    disable: bool = typer.Option(False, "--disable", help="禁用任务而非启用"),
):
    """启用或禁用一个任务。"""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="需要运行的任务 ID"),
    force: bool = typer.Option(False, "--force", "-f", help="即使任务已禁用也强制运行"),
):
    """手动立即运行指定的任务。"""
    from loguru import logger

    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    logger.disable("nanobot")

    config = load_config()
    provider = _make_provider(config)
    bus = MessageBus()
    agent_loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        llm_timeout_seconds=config.agents.defaults.llm_timeout_seconds,
        stage_heartbeat_seconds=config.agents.defaults.stage_heartbeat_seconds,
        skillspec_render_primary_timeout_seconds=config.agents.defaults.skillspec_render_primary_timeout_seconds,
        skillspec_render_retry_timeout_seconds=config.agents.defaults.skillspec_render_retry_timeout_seconds,
        brave_api_key=config.tools.web.search.api_key or None,
        exec_config=config.tools.exec,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        feishu_data_config=config.tools.feishu_data,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
        response_template_config=config.agents.response_templates,
        skillspec_config=config.agents.skillspec,
        skillspec_embedding_provider_config=config.providers.siliconflow,
    )

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    result_holder = []

    async def on_job(job: CronJob) -> str | None:
        response = await agent_loop.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        result_holder.append(response)
        return response

    service.on_job = on_job

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
        if result_holder:
            _print_agent_response(result_holder[0], render_markdown=True)
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# 状态检查命令 (Status Commands)
# ============================================================================


@app.command()
def status():
    """显示 nanobot 的工作区及配置状态。"""
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

        # 从注册表中检查 API 配置情况
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # 本地部署模型时显示其 api_base 而不是 api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


# ============================================================================
# OAuth 授权登录 (OAuth Login)
# ============================================================================

provider_app = typer.Typer(help="管理模型 API 及其授权")
app.add_typer(provider_app, name="provider")


_LOGIN_HANDLERS: dict[str, callable] = {}


def _register_login(name: str):
    def decorator(fn):
        _LOGIN_HANDLERS[name] = fn
        return fn
    return decorator


@provider_app.command("login")
def provider_login(
    provider: str = typer.Argument(..., help="OAuth 提供商名称 (例如 'openai-codex', 'github-copilot')"),
):
    """使用指定的 OAuth 提供商进行授权验证。"""
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
            console.print("[cyan]正在启动交互式 OAuth 登录授权...[/cyan]\n")
            token = login_oauth_interactive(
                print_fn=lambda s: console.print(s),
                prompt_fn=lambda s: typer.prompt(s),
            )
        if not (token and token.access):
            console.print("[red]✗ 授权验证失败[/red]")
            raise typer.Exit(1)
        console.print(f"[green]✓ 已成功授权访问 OpenAI Codex[/green]  [dim]{token.account_id}[/dim]")
    except ImportError:
        console.print("[red]尚未安装 oauth_cli_kit，请运行：pip install oauth-cli-kit[/red]")
        raise typer.Exit(1)


@_register_login("github_copilot")
def _login_github_copilot() -> None:
    import asyncio

    console.print("[cyan]正在启动 GitHub Copilot 设备授权流...[/cyan]\n")

    async def _trigger():
        from litellm import acompletion
        await acompletion(model="github_copilot/gpt-4o", messages=[{"role": "user", "content": "hi"}], max_tokens=1)

    try:
        asyncio.run(_trigger())
        console.print("[green]✓ 已成功授权访问 GitHub Copilot[/green]")
    except Exception as e:
        console.print(f"[red]授权验证错误：{e}[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
