from pydantic import BaseModel


class Config(BaseModel):
    """Plugin Config Here"""
    peteralbus_wife_res: str = "/home/PeterAlbus/napcat/resources/peteralbus_wife"
