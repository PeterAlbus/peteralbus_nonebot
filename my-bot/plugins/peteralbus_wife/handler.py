from nonebot import on_command, require
from nonebot.rule import to_me
from . import config
from pathlib import Path
import random
from nonebot.log import logger
from typing import Dict, List, Optional, Tuple, Union
from datetime import date, datetime
from nonebot.adapters.onebot.v11 import (
    GROUP,
    GROUP_ADMIN,
    GROUP_OWNER,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # isort:skip

class WifeManager:
    def __init__(self):
        self.user_info = {}

    def record_wife(self, gid: str, uid: str, character_name: str, image_path: str):
        """
        记录老婆信息
        """
        if gid not in self.user_info:
            self.user_info[gid] = {}
        self.user_info[gid][uid] = {
            "character_name": character_name,
            "image_path": image_path
        }
    
    def get_wife(self, gid: str, uid: str) -> Optional[Dict[str, str]]:
        return self.user_info.get(gid, {}).get(uid, None)
    
    def clean(self):
        self.user_info = {}

wife_manager = WifeManager()

today_wife = on_command("今日老婆", rule=to_me(), aliases={"老婆"}, priority=10, block=True)

def random_wife_pic():
    """
    随机获取一张图片
    """
    menu_dir = Path("/home/PeterAlbus/napcat/resources/peteralbus_wife")
    # 获取所有文件夹（角色名）
    character_folders = [folder for folder in menu_dir.iterdir() if folder.is_dir()]

    if not character_folders:
        logger.error("没有找到角色文件夹")
        return None, None
    
    # 随机选择一个角色
    random_character_folder = random.choice(character_folders)
    # 获取该角色文件夹内所有图片文件
    image_files = [file for file in random_character_folder.iterdir() if file.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif']]
    
    if not image_files:
        logger.error(f"角色 {random_character_folder.name} 中没有图片文件")
        return None, None
    
    # 随机选择一张图片
    random_image = random.choice(image_files)
    return random_character_folder.name, random_image

def get_agnes_pic():
    path_list = [
        "/home/PeterAlbus/napcat/resources/peteralbus_wife/亚妮艾斯",
    ]
    
    # 随机选择一个角色
    random_character_folder = Path(random.choice(path_list))
    # 获取该角色文件夹内所有图片文件
    image_files = [file for file in random_character_folder.iterdir() if file.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif']]
    
    if not image_files:
        logger.error(f"角色 {random_character_folder.name} 中没有图片文件")
        return None, None
    
    # 随机选择一张图片
    random_image = random.choice(image_files)
    return random_character_folder.name, random_image

@today_wife.handle()
async def today_wife_handle(event: GroupMessageEvent):
    gid: str = str(event.group_id)
    uid: str = str(event.user_id)
    character_name = None
    image_path = None
    is_new = True
    if wife_manager.get_wife(gid, uid):
        character_name = wife_manager.get_wife(gid, uid)["character_name"]
        image_path = wife_manager.get_wife(gid, uid)["image_path"]
        is_new = False
    else:
        character_name, image_path = random_wife_pic()
        if uid == "2997592724":
            character_name, image_path = get_agnes_pic()
        wife_manager.record_wife(gid, uid, character_name, image_path)

    if character_name is None or image_path is None:
        await today_wife.finish("寻找老婆出错了...")
    # 发送角色名和图片
    if is_new:
        message = MessageSegment.text(f"今日老婆：{character_name}!") + MessageSegment.image(image_path)
    else:
        message = MessageSegment.text(f"今日老婆：{character_name}。") + MessageSegment.image(image_path)
    # 发送图片路径
    await today_wife.finish(message, at_sender=True)


@scheduler.scheduled_job("cron", hour=0, minute=0, misfire_grace_time=60)
async def _():
    wife_manager.clean()
    logger.info("昨日老婆信息已清空！")