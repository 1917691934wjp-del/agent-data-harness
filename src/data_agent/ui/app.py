"""Chainlit UI application for Data Agent."""

import base64
import asyncio
import logging
import os
from typing import cast

import chainlit as cl
from chainlit.element import Element
from dotenv import load_dotenv
import pandas as pd
from jwt import InvalidTokenError

from data_agent.agent import DataAgentFlow
from data_agent.config import CONFIG_DIR
from data_agent.config_loader import ConfigLoader
from data_agent.core.logging import setup_logging
from data_agent.harness import AgentHarness, RuntimePolicy
from data_agent.ui.accounts import AccountStore
from data_agent.ui.account_routes import install_accounts
from data_agent.models.collaboration import AgentTaskResult
from data_agent.ui.history import LocalHistory, open_checkpointer, register_file_route
from data_agent.utils.review import human_reply

setup_logging()
logger = logging.getLogger(__name__)

load_dotenv()

history = LocalHistory()
register_file_route(history)
accounts = AccountStore(history.root)
accounts.bootstrap(f.stem for f in CONFIG_DIR.glob("*.yaml"))
install_accounts(lambda: accounts)


@cl.data_layer
def get_history():
    return history


@cl.password_auth_callback
async def authenticate(username: str, password: str):
    return await asyncio.to_thread(accounts.verify, username, password)


@cl.on_logout
async def logout(request, response):
    from chainlit.auth import decode_jwt, get_token_from_cookies

    try:
        user = decode_jwt(get_token_from_cookies(request.cookies))
        await asyncio.to_thread(accounts.revoke, user.identifier)
    except (ValueError, TypeError, InvalidTokenError):
        pass
    return {"success": True}


CONFIGS = {f.stem: f for f in CONFIG_DIR.glob("*.yaml")}

CONFIG_DESCRIPTIONS = {
    "all": "自动选择数据助手，查询已获授权的数据源。",
    "contoso": "查询零售业务的商品、客户、订单和库存数据。",
    "amex": "查询客户账户、金融交易和风险预警数据。",
    "adventure_works": "查询员工、人事和酒店预订经营数据。",
}

PROFILE_NAMES = {
    "all_agents": "全部数据助手",
    "contoso": "零售与库存",
    "amex": "金融交易",
    "adventure_works": "人事与酒店经营",
}
STATUS_NAMES = {
    "success": "成功",
    "error": "失败",
    "timeout": "超时",
    "blocked": "未执行",
}


def resolve_profile(name: str) -> str:
    """Map localized labels to stable config IDs, retaining old chat profiles."""
    return next(
        (key for key, label in PROFILE_NAMES.items() if label == name),
        name.lower().replace(" ", "_"),
    )


CONFIG_ICONS = {
    "all": "https://img.icons8.com/fluency/96/data-configuration.png",
    "contoso": "https://img.icons8.com/fluency/96/shop.png",
    "amex": "https://img.icons8.com/color/96/amex.png",
    "adventure_works": "https://img.icons8.com/fluency/96/mountain.png",
}


def get_azure_credentials() -> tuple[str, str, str]:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    return endpoint, api_key, deployment


@cl.set_chat_profiles
async def chat_profiles(user: cl.User | None = None, thread_id: str | None = None):
    try:
        allowed = accounts.profiles(user)
    except ValueError:
        return []
    profiles = []

    profiles.append(
        cl.ChatProfile(
            name="全部数据助手",
            markdown_description=f"**全部数据助手**\n\n{CONFIG_DESCRIPTIONS.get('all', '')}",
            icon=CONFIG_ICONS.get(
                "all", "https://img.icons8.com/fluency/96/data-configuration.png"
            ),
            default=True,
        )
    )

    for name in sorted(CONFIGS.keys() & allowed):
        description = CONFIG_DESCRIPTIONS.get(name, f"使用中文向 {name} 数据助手提问。")
        icon = CONFIG_ICONS.get(name, "https://img.icons8.com/fluency/96/database.png")
        profiles.append(
            cl.ChatProfile(
                name=PROFILE_NAMES.get(name, name),
                markdown_description=f"**{PROFILE_NAMES.get(name, name)}**\n\n{description}",
                icon=icon,
                default=False,
            )
        )
    return profiles


@cl.on_chat_start
async def on_chat_start():
    await initialize_chat()


async def initialize_chat(thread=None):
    """Recreate connections around the same durable conversation checkpoint."""
    if cl.user_session.get("saver_context"):
        await on_chat_end()
    user = cl.user_session.get("user")
    try:
        allowed = accounts.profiles(user)
    except ValueError:
        await cl.Message(content="登录已失效，请重新登录。").send()
        return
    if not allowed:
        await cl.Message(
            content="账号已创建，请联系管理员分配数据源权限后重新登录。账号管理：/account"
        ).send()
        return
    if thread and await history.get_thread_author(thread["id"]) != user.identifier:
        raise PermissionError("无权恢复其他用户的会话。")
    if thread:
        metadata = thread.get("metadata") or {}
        if metadata.get("chat_profile"):
            cl.user_session.set("chat_profile", metadata["chat_profile"])
    chat_profile = cl.user_session.get("chat_profile")
    if not chat_profile:
        chat_profile = "全部数据助手"

    config_name = resolve_profile(chat_profile)

    if config_name == "all_agents":
        try:
            configs = [
                ConfigLoader.load(CONFIGS[name])
                for name in sorted(allowed & CONFIGS.keys())
            ]
            if not configs:
                raise ValueError("没有已授权的数据源配置")
            config = configs[0]
            config.data_agents = [
                agent for value in configs for agent in value.data_agents
            ]
            display_name = "全部数据助手"
        except Exception as e:
            await cl.Message(content=f"加载配置失败： {e}").send()
            return
    else:
        if config_name not in allowed:
            await cl.Message(content="无权访问该数据源，请联系管理员。").send()
            return
        config_path = CONFIGS.get(config_name)
        if not config_path:
            await cl.Message(
                content=f"未找到配置“{config_name}”。可用配置： {', '.join(CONFIGS.keys())}"
            ).send()
            return
        try:
            config = ConfigLoader.load(config_path)
            display_name = chat_profile
        except Exception as e:
            await cl.Message(content=f"加载配置失败： {e}").send()
            return

    endpoint, api_key, deployment = get_azure_credentials()
    providers = {
        config.intent_detection.llm_config.provider,
        *(agent.llm_config.provider for agent in config.data_agents),
    }
    if "deepseek" in providers and not os.getenv("DEEPSEEK_API_KEY", "").strip():
        await cl.Message(
            content="尚未配置 DeepSeek，请在 .env 中填写 DEEPSEEK_API_KEY。"
        ).send()
        return
    if "azure_openai" in providers and (not endpoint or not api_key):
        await cl.Message(
            content="尚未配置模型服务，请在 .env 中填写 AZURE_OPENAI_ENDPOINT 和 AZURE_OPENAI_API_KEY。"
        ).send()
        return

    saver_context = None
    flow = None
    try:
        thread_id = thread["id"] if thread else cl.context.session.thread_id
        persisted = await history.get_user(user.identifier)
        if persisted is None:
            persisted = await history.create_user(user)
        await history.update_thread(
            thread_id,
            user_id=persisted.id,
            metadata={"chat_profile": chat_profile, "config_name": config_name},
        )
        graph_thread_id = await history.checkpoint_id(thread_id, user.identifier)
        saver_context = open_checkpointer(history.root)
        saver = await saver_context.__aenter__()
        await saver.setup()
        flow = DataAgentFlow(
            config=config,
            azure_endpoint=endpoint,
            api_key=api_key,
            deployment_name=deployment,
            checkpointer=saver,
        )
        await flow.connect()

        cl.user_session.set("flow", flow)
        cl.user_session.set("saver_context", saver_context)
        cl.user_session.set("config_name", config_name)
        cl.user_session.set("thread_id", thread_id)
        cl.user_session.set("graph_thread_id", graph_thread_id)
        cl.user_session.set(
            "authorized_agents", frozenset(agent.name for agent in config.data_agents)
        )
        pending = await flow.get_pending_input(graph_thread_id)
        cl.user_session.set(
            "awaiting_clarification",
            value=bool(pending and pending.get("type") == "clarification_needed"),
        )
        cl.user_session.set("pending_input", value=pending)
        user = cl.user_session.get("user")
        if user:
            persisted = await history.get_user(user.identifier)
            if persisted is None:
                persisted = await history.create_user(user)
            await history.update_thread(
                thread_id,
                user_id=persisted.id,
                metadata={"chat_profile": chat_profile, "config_name": config_name},
            )

        if thread:
            if pending:
                await cl.Message(
                    content=(
                        f"已恢复历史对话。\n\n{pending.get('message', '请继续确认查询计划。')}"
                        f"\n\n提示：{pending.get('hint', '')}"
                    )
                ).send()
            return

        agents = flow.get_agent_names()

        await cl.Message(
            content=f"👋 已连接到 **{display_name}**！\n\n"
            f"可用数据助手： {', '.join(f'`{a}`' for a in agents)}\n\n"
            f"直接用中文提问，例如：每个仓库分别有多少库存？"
        ).send()

    except Exception as e:
        cl.user_session.set("flow", None)
        cl.user_session.set("saver_context", None)
        if flow:
            await flow.disconnect()
        if saver_context:
            await saver_context.__aexit__(None, None, None)
        await cl.Message(content=f"初始化失败： {e}").send()


@cl.on_chat_end
async def on_chat_end():
    flow = cl.user_session.get("flow")
    try:
        if flow:
            await flow.disconnect()
    finally:
        cl.user_session.set("flow", None)
        saver_context = cl.user_session.get("saver_context")
        if saver_context:
            cl.user_session.set("saver_context", None)
            await saver_context.__aexit__(None, None, None)


@cl.on_message
async def on_message(message: cl.Message):
    user = cl.user_session.get("user")
    try:
        accounts.validate(user)
        import chainlit.auth as auth

        auth.decode_jwt(cl.context.session.token)
    except (ValueError, TypeError, InvalidTokenError):
        await on_chat_end()
        await cl.Message(content="登录已失效，请重新登录。").send()
        return
    flow: DataAgentFlow | None = cl.user_session.get("flow")
    if not flow:
        await cl.Message(content="尚未连接数据源，请刷新页面重新连接。").send()
        return

    thread_id = cl.user_session.get("graph_thread_id")
    policy = RuntimePolicy.from_environment()
    allowed_agents = cl.user_session.get("authorized_agents", frozenset())
    if policy.allowed_agents is not None:
        allowed_agents &= policy.allowed_agents
    policy = policy.model_copy(update={"allowed_agents": allowed_agents})

    try:
        async with cl.Step(
            name="正在分析问题并查询数据…", type="run", show_input=False
        ) as thinking_step:
            pending = cl.user_session.get("pending_input")
            if pending:
                result = await AgentHarness(flow, policy=policy).run_output(
                    thread_id=thread_id,
                    resume_value=human_reply(pending, message.content),
                )
            else:
                result = await AgentHarness(flow, policy=policy).run_output(
                    message.content, thread_id=thread_id
                )
            cl.user_session.set("awaiting_clarification", value=False)
            cl.user_session.set("pending_input", value=None)
            thinking_step.output = "处理完成"

        logger.info(f"Result type: {type(result)}, Result: {result}")

        if isinstance(result, dict) and "__interrupt__" in result:
            interrupt_data = result.get("__interrupt__", [])
            if interrupt_data:
                interrupt_obj = interrupt_data[0]
                payload = (
                    interrupt_obj.value
                    if hasattr(interrupt_obj, "value")
                    else interrupt_obj
                )
                hint = payload.get("hint", "")
                cl.user_session.set("pending_input", value=payload)
                cl.user_session.set(
                    "awaiting_clarification",
                    value=payload.get("type") == "clarification_needed",
                )
                await cl.Message(
                    content=f"⚠️ {payload.get('message', '请补充说明你的问题。')}\n\n*提示：{hint}*"
                ).send()
            return

        if isinstance(result, dict):
            datasource_name = result.get("datasource_name", "") or ""
            generated_sql = result.get("generated_sql", "") or ""
            query_result = result.get("result", {}) or {}
            final_response = result.get("final_response", "") or ""
            error = result.get("error") or None
            visualization_image = result.get("visualization_image") or None
            visualization_code = result.get("visualization_code") or None
            visualization_error = result.get("visualization_error") or None
        else:
            datasource_name = getattr(result, "datasource_name", "") or ""
            generated_sql = getattr(result, "generated_sql", "") or ""
            query_result = getattr(result, "result", {}) or {}
            final_response = getattr(result, "final_response", "") or ""
            error = getattr(result, "error", None)
            visualization_image = getattr(result, "visualization_image", None)
            visualization_code = getattr(result, "visualization_code", None)
            visualization_error = getattr(result, "visualization_error", None)

        logger.info(
            f"Parsed - datasource: {datasource_name}, sql: {bool(generated_sql)}, "
            f"result: {bool(query_result)}, response: {bool(final_response)}, "
            f"viz: {bool(visualization_image)}, error: {error}"
        )

        if datasource_name:
            async with cl.Step(
                name=f"🔀 已分配给： {datasource_name}", type="run", show_input=False
            ) as route_step:
                route_step.output = f"由 **{datasource_name}** 数据助手处理此问题"

        if generated_sql:
            async with cl.Step(
                name="📝 查询 SQL", type="tool", show_input=False
            ) as sql_step:
                sql_step.output = f"```sql\n{generated_sql}\n```"

        if isinstance(result, dict):
            await show_agent_results(result.get("agent_results", []))

        if query_result:
            if isinstance(query_result, dict):
                rows = query_result.get("rows", [])
                columns = query_result.get("columns", [])
            else:
                rows = getattr(query_result, "rows", [])
                columns = getattr(query_result, "columns", [])

            if columns and rows:
                df = pd.DataFrame(rows, columns=columns)
                async with cl.Step(
                    name="📊 原始查询结果",
                    type="tool",
                    show_input=False,
                ) as results_step:
                    elements = [cl.Dataframe(data=df, name="原始查询结果")]
                    results_step.elements = cast("list[Element]", elements)
                    results_step.output = df.to_markdown(index=False)

        if final_response:
            await cl.Message(content=final_response).send()
        elif error:
            error_msg = str(error)
            if error == "out_of_scope":
                await cl.Message(
                    content="🚫 当前数据无法回答这个问题，请围绕上方列出的数据源提问。"
                ).send()
            else:
                await cl.Message(content=f"查询失败：{error_msg}").send()
        else:
            await cl.Message(
                content="查询已完成，但未生成回答，请换一种方式描述问题。"
            ).send()

        if visualization_code:
            async with cl.Step(
                name="🐍 图表生成代码", type="tool", show_input=False
            ) as code_step:
                code_step.output = f"```python\n{visualization_code}\n```"

        if visualization_image:
            try:
                # Decode base64 to bytes
                image_bytes = base64.b64decode(visualization_image)

                elements = [
                    cl.Image(
                        name="chart",
                        content=image_bytes,
                        display="inline",
                        size="large",
                    )
                ]
                await cl.Message(
                    content="📊 数据图表：",
                    elements=cast("list[Element]", elements),
                ).send()
            except Exception as e:
                logger.error(f"Failed to render visualization: {e}")
                await cl.Message(content=f"⚠️ 图表显示失败： {e}").send()

        # Handle visualization errors
        if visualization_error:
            await cl.Message(content=f"⚠️ 无法生成图表： {visualization_error}").send()

    except Exception as e:
        logger.exception("Error processing message")
        await cl.Message(content=f"处理时发生错误： {e}").send()


@cl.on_chat_resume
async def on_chat_resume(thread):
    await initialize_chat(thread)


async def show_agent_results(results: list) -> None:
    """Expose source provenance and dependency exchanges for collaborative queries."""
    if len(results) <= 1:
        return
    for value in results:
        task = AgentTaskResult.model_validate(value)
        await show_agent_result(task)


async def show_agent_result(task: AgentTaskResult) -> None:
    """Render one source's evidence without combining unrelated row schemas."""
    async with cl.Step(name=f"{task.task_id} · {task.agent_name}", type="tool") as step:
        lines = [
            f"任务：{task.question}",
            f"状态：{STATUS_NAMES.get(task.status, task.status)}",
        ]
        if task.depends_on:
            lines.append(f"使用以下任务的查询结果： {', '.join(task.depends_on)}")
        if task.error:
            lines.append(f"错误：{task.error}")
        if task.generated_sql:
            lines.append(f"```sql\n{task.generated_sql}\n```")
        if task.result is not None:
            lines.append(f"返回记录数： {task.result.row_count}")
            if task.result.columns:
                frame = pd.DataFrame(task.result.rows, columns=task.result.columns)
                step.elements = [cl.Dataframe(data=frame, name=task.task_id)]
        step.output = "\n\n".join(lines)


def main() -> None:
    """Run the Chainlit UI application."""
    from pathlib import Path
    import subprocess
    import sys

    app_path = str(Path(__file__).resolve())
    subprocess.run(
        [
            sys.executable,
            "-m",
            "chainlit",
            "run",
            app_path,
            "--host",
            "localhost",
            "--port",
            "8000",
            "-w",
        ],
        check=True,
    )
