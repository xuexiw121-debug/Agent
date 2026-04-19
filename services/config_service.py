import os
import re
from pathlib import Path

import folium
import streamlit as st
from dotenv import load_dotenv

DAY_COLORS = [
    [244, 67, 54, 210],
    [33, 150, 243, 210],
    [76, 175, 80, 210],
    [255, 152, 0, 210],
    [156, 39, 176, 210],
    [0, 188, 212, 210],
    [121, 85, 72, 210],
]

CITY_ALIAS_MAP = {
    "帝都": "北京",
    "魔都": "上海",
    "羊城": "广州",
    "鹏城": "深圳",
    "蓉城": "成都",
    "山城": "重庆",
    "冰城": "哈尔滨",
    "津门": "天津",
    "杭州城": "杭州",
    "姑苏": "苏州",
}


def load_env_from_root(app_file: str) -> None:
    base_dir = Path(app_file).resolve().parent
    load_dotenv(dotenv_path=base_dir / ".env", override=False)


def _resolve_key(name: str) -> str:
    secret_key = ""
    try:
        secret_key = st.secrets.get(name, "")
    except Exception:
        secret_key = ""
    env_key = os.getenv(name, "")
    raw_key = secret_key or env_key
    return raw_key.strip().strip('"').strip("'")


def resolve_dashscope_api_key() -> str:
    return _resolve_key("DASHSCOPE_API_KEY")


def resolve_amap_api_key() -> str:
    return _resolve_key("AMAP_API_KEY")


def resolve_model_name() -> str:
    return os.getenv("DASHSCOPE_MODEL", "qwen3-max")


def mask_key(api_key: str) -> str:
    if not api_key:
        return "未读取到"
    if len(api_key) <= 10:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def normalize_destination_name(raw_name: str) -> str:
    name = str(raw_name or "").strip()
    if not name:
        return ""
    if name in CITY_ALIAS_MAP:
        return CITY_ALIAS_MAP[name]
    name = re.sub(r"(市|地区|自治州|特别行政区)$", "", name)
    return name.strip()


def day_color(day_no: int) -> list:
    if isinstance(day_no, int) and day_no > 0:
        return DAY_COLORS[(day_no - 1) % len(DAY_COLORS)]
    return DAY_COLORS[0]


def rgb_to_hex(color: list) -> str:
    if not isinstance(color, list) or len(color) < 3:
        return "#2196f3"
    r, g, b = color[0], color[1], color[2]
    return f"#{int(r):02x}{int(g):02x}{int(b):02x}"


def add_chinese_tiles(m: folium.Map) -> None:
    folium.TileLayer(
        tiles="https://webrd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}",
        attr="高德地图",
        name="高德中文",
        overlay=False,
        control=False,
    ).add_to(m)
