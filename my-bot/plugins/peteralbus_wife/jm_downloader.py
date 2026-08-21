import asyncio
import shutil
import time
from pathlib import Path
from typing import Optional, Set, Tuple
from uuid import uuid4

from jmcomic import Feature, JmOption, create_option_by_file, download_album_async
from nonebot import get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import GROUP, Bot, GroupMessageEvent, Message
from nonebot.log import logger
from nonebot.params import CommandArg

from .config import Config


config = get_plugin_config(Config)

download = on_command(
    "jm",
    aliases={"jm下载", "JM"},
    permission=GROUP,
    priority=5,
    block=True,
)

_active_jobs: Set[str] = set()
_download_semaphore = asyncio.Semaphore(
    max(1, config.peteralbus_wife_jm_max_concurrency)
)


def _resolve_option_path() -> Path:
    option_path = Path(config.peteralbus_wife_jm_option_path).expanduser()
    if not option_path.is_absolute():
        option_path = Path(__file__).parent / option_path
    return option_path.resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _prepare_job(jm_code: str) -> Tuple[JmOption, Path, Path]:
    option_path = _resolve_option_path()
    if not option_path.is_file():
        raise FileNotFoundError(f"JM 配置文件不存在: {option_path}")

    option = create_option_by_file(str(option_path))
    configured_work_dir = config.peteralbus_wife_jm_work_dir.strip()
    if configured_work_dir:
        work_root = Path(configured_work_dir).expanduser()
        if not work_root.is_absolute():
            work_root = Path(option.dir_rule.base_dir) / work_root
    else:
        work_root = Path(option.dir_rule.base_dir) / ".nonebot_tasks"

    work_root = work_root.resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    job_dir = (work_root / f"jm-{jm_code}-{uuid4().hex}").resolve()
    if not _is_within(job_dir, work_root):
        raise ValueError("JM 临时任务目录越过工作目录")
    job_dir.mkdir()

    # 每个命令使用独立根目录，避免并发任务扫描、覆盖或删除彼此文件。
    option.dir_rule.base_dir = str(job_dir)
    return option, work_root, job_dir


def _cleanup_job_dir(job_dir: Path, work_root: Path) -> None:
    if job_dir.is_dir() and _is_within(job_dir, work_root):
        shutil.rmtree(job_dir)


def _cleanup_stale_jobs(work_root: Path) -> None:
    retention_hours = max(0, config.peteralbus_wife_jm_failed_retention_hours)
    # 即使失败文件配置为不保留，也给正在运行的并发任务留出安全窗口。
    cutoff = time.time() - max(1, retention_hours) * 3600
    if not work_root.is_dir():
        return

    for child in work_root.iterdir():
        try:
            if (
                child.is_dir()
                and child.name.startswith("jm-")
                and _is_within(child, work_root)
                and child.stat().st_mtime < cutoff
            ):
                shutil.rmtree(child)
        except OSError:
            logger.exception("清理过期 JM 任务目录失败: {}", child)


def _select_pdf_path(result, job_dir: Path) -> Path:
    pdf_paths = [
        Path(path).resolve()
        for path in result.manifest.get_export_filepath_list("pdf")
    ]
    valid_paths = [
        path for path in pdf_paths if path.is_file() and _is_within(path, job_dir)
    ]
    if len(valid_paths) != 1:
        raise RuntimeError(
            f"预期生成 1 个 PDF，实际找到 {len(valid_paths)} 个"
        )
    return valid_paths[0]


def _format_public_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    if len(message) > 180:
        message = f"{message[:177]}..."
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


async def _send_status(message: str) -> None:
    try:
        await download.send(message)
    except Exception:
        logger.exception("发送 JM 任务状态失败: {}", message)


def _is_allowed(event: GroupMessageEvent) -> bool:
    allowed_groups = set(config.peteralbus_wife_jm_allowed_groups)
    allowed_users = set(config.peteralbus_wife_jm_allowed_users)
    return (
        not allowed_groups or str(event.group_id) in allowed_groups
    ) and (
        not allowed_users or str(event.user_id) in allowed_users
    )


@download.handle()
async def handle_jm_download(
    bot: Bot,
    event: GroupMessageEvent,
    args: Message = CommandArg(),
) -> None:
    jm_code = args.extract_plain_text().strip()
    if not jm_code.isdigit():
        await download.finish("请提供要下载的 JM 号，例如：/jm 350234")
    if not _is_allowed(event):
        await download.finish("当前群聊或用户没有使用 JM 下载功能的权限。")
    if jm_code in _active_jobs:
        await download.finish(f"JM{jm_code} 已在下载中，请勿重复提交。")

    _active_jobs.add(jm_code)
    work_root: Optional[Path] = None
    job_dir: Optional[Path] = None
    upload_succeeded = False

    try:
        if _download_semaphore.locked():
            await _send_status(f"JM{jm_code} 已进入下载队列。")
        else:
            await _send_status(f"开始下载 JM{jm_code}，完成后将上传 PDF。")

        async with _download_semaphore:
            option, work_root, job_dir = _prepare_job(jm_code)
            await asyncio.to_thread(_cleanup_stale_jobs, work_root)

            pdf_feature = Feature.export_pdf(
                pdf_dir=str(job_dir / "pdf"),
                filename_rule="[JM{Aid}] {Atitle}",
                delete_original_file=False,
            )
            result = await asyncio.wait_for(
                download_album_async(jm_code, option, extra=pdf_feature),
                timeout=max(1, config.peteralbus_wife_jm_download_timeout),
            )
            pdf_path = _select_pdf_path(result, job_dir)

            max_pdf_mb = max(0, config.peteralbus_wife_jm_max_pdf_mb)
            if max_pdf_mb and pdf_path.stat().st_size > max_pdf_mb * 1024 * 1024:
                raise RuntimeError(f"PDF 超过 {max_pdf_mb} MiB 的上传限制")

            await asyncio.wait_for(
                bot.call_api(
                    "upload_group_file",
                    group_id=event.group_id,
                    file=str(pdf_path),
                    name=pdf_path.name,
                ),
                timeout=max(1, config.peteralbus_wife_jm_upload_timeout),
            )
            upload_succeeded = True

        logger.info(
            "JM 下载并上传成功: jm_id={}, group_id={}, user_id={}, duration={:.2f}s",
            jm_code,
            event.group_id,
            event.user_id,
            result.duration or 0,
        )
        await _send_status(f"JM{jm_code} 下载并上传完成。")
    except asyncio.TimeoutError:
        logger.exception(
            "JM 下载或上传超时: jm_id={}, group_id={}",
            jm_code,
            event.group_id,
        )
        await _send_status(f"JM{jm_code} 处理超时，请稍后重试。")
    except Exception as error:
        logger.exception(
            "JM 下载或上传失败: jm_id={}, group_id={}",
            jm_code,
            event.group_id,
        )
        await _send_status(f"JM{jm_code} 处理失败：{_format_public_error(error)}")
    finally:
        _active_jobs.discard(jm_code)
        if job_dir is not None and work_root is not None:
            retention_hours = max(
                0, config.peteralbus_wife_jm_failed_retention_hours
            )
            if upload_succeeded or retention_hours == 0:
                try:
                    await asyncio.to_thread(_cleanup_job_dir, job_dir, work_root)
                except Exception:
                    logger.exception("清理 JM 任务目录失败: {}", job_dir)
            else:
                logger.warning(
                    "JM 失败任务文件将保留 {} 小时: jm_id={}, task_dir={}",
                    retention_hours,
                    jm_code,
                    job_dir,
                )
