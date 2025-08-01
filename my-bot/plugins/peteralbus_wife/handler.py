from nonebot import on_command
from nonebot.rule import to_me
from .config import config
from pathlib import Path
import random
from nonebot.log import logger
from nonebot import adapter
from nonebot.adapters.onebot.v11 import (
    GROUP,
    GROUP_ADMIN,
    GROUP_OWNER,
    GroupMessageEvent,
    Message,
    MessageSegment,
)

today_wife = on_command("今日老婆", rule=to_me(), aliases={"老婆"}, priority=10, block=True)

def random_wife_pic():
    """
    随机获取一张图片
    """
    menu_dir = Path(config.whatpic_res_path)
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

@today_wife.handle()
async def today_wife_handle(event: GroupMessageEvent):
    # 获取随机的角色名和图片
    character_name, image_path = random_wife_pic()

    if character_name is None or image_path is None:
        await today_wife.finish("寻找老婆出错了...")
    # 发送角色名和图片
    message = MessageSegment.text(f"今日老婆：{character_name}") + MessageSegment.image(image_path)
    # 发送图片路径
    await today_wife.finish(message, at_sender=True)