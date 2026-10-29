"""
common.py —— LitPulse 各脚本共享的配置加载
==========================================
把"读 .env / 环境变量 / 教学环境回退"的逻辑集中放这里，
weekly_pulse.py、reader.py、compare.py 和网页 app.py 都复用，避免重复代码。

对大众用户：不用懂环境变量，在网页侧边栏填一次 Key 即可，
本模块会把它写进项目目录下的 .env 并即时生效。
"""

import os
from openai import OpenAI

ENV_PATH = ".env"
DEFAULT_MODEL = "deepseek-chat"

# 常见 OpenAI 兼容服务的一键预设（网页设置里可选）
PROVIDERS = [
    {"name": "DeepSeek",
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"name": "火山方舟（豆包）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-pro-32k"},
    {"name": "OpenAI 官方（需能访问）",
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"name": "自定义（其它 OpenAI 兼容接口）",
     "base_url": "", "model": ""},
]


class ConfigError(Exception):
    """尚未配置模型 Key 时抛出，携带面向用户的中文提示。"""


def load_dotenv(path=ENV_PATH):
    """极简 .env 解析：每行 KEY=VALUE，不引入额外依赖。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _read_env_file(path=ENV_PATH):
    """读出 .env 现有内容为 dict（不碰环境变量），用于保存时保留其它键。"""
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def save_config(api_key, base_url, model, path=ENV_PATH):
    """把模型配置写入 .env（保留文件里已有的其它键），并立即更新进程环境。"""
    data = _read_env_file(path)
    data["LITPULSE_API_KEY"] = api_key.strip()
    # base_url 允许为空（OpenAI 官方 SDK 会用默认），但通常要填
    if base_url and base_url.strip():
        data["LITPULSE_BASE_URL"] = base_url.strip()
    else:
        data.pop("LITPULSE_BASE_URL", None)
    data["LITPULSE_MODEL"] = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    lines = ["# LitPulse 配置（由网页设置自动生成；请勿提交到公开仓库/分享给他人）"]
    for k, v in data.items():
        lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # 即时生效（setdefault 不行，因为键可能已存在，这里强制覆盖）
    os.environ["LITPULSE_API_KEY"] = data["LITPULSE_API_KEY"]
    if "LITPULSE_BASE_URL" in data:
        os.environ["LITPULSE_BASE_URL"] = data["LITPULSE_BASE_URL"]
    os.environ["LITPULSE_MODEL"] = data["LITPULSE_MODEL"]
    # 清掉可能残留的旧的标准变量名，避免读到旧值
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("OPENAI_BASE_URL", None)


def get_raw_config():
    """返回 (api_key, base_url, model)。读取顺序：.env / 环境变量。"""
    load_dotenv()
    api_key = os.environ.get("LITPULSE_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("LITPULSE_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    model = os.environ.get("LITPULSE_MODEL", DEFAULT_MODEL)

    return api_key, base_url, model


def is_configured():
    """是否已具备可用的模型配置（用户自己填了 Key，或处于教学机回退环境）。"""
    api_key, _b, _m = get_raw_config()
    return bool(api_key)


def active_provider_hint():
    """给网页一个简短的当前配置描述（不泄露完整 key）。"""
    api_key, base_url, model = get_raw_config()
    if not api_key:
        return "未配置"
    tail = (api_key[-4:] if len(api_key) > 4 else "****")
    via = base_url or "OpenAI 默认"
    return f"模型 {model} ｜ {via} ｜ Key 末尾 …{tail}"


def load_config():
    """返回 (client, model)。未配置时抛 ConfigError，由界面给中文提示。"""
    api_key, base_url, model = get_raw_config()
    if not api_key:
        raise ConfigError(
            "还没有配置模型 Key。请在左侧边栏【模型设置】里填一次，"
            "保存后即可使用。")
    client = OpenAI(base_url=base_url or None, api_key=api_key)
    return client, model
