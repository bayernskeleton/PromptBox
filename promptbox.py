"""
PromptBox - 便携提示词箱
Ctrl+Shift+Space 唤起，文件夹式分类管理提示词，支持 {占位符} 填充、
点击式标签筛选、智能标签推荐、复制/粘贴到光标位置。

依赖: keyboard, pyperclip, tkinter (标准库)
"""

import json
import os
import re
import sys
import subprocess
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog
except ImportError:  # headless/test environments can still load data functions
    tk = None
    ttk = None
    messagebox = None
    simpledialog = None
try:
    import keyboard
except ImportError:
    keyboard = None
try:
    import pyperclip
except ImportError:
    pyperclip = None
try:
    from PIL import Image, ImageTk  # 支持 RGBA/PNG，tk.PhotoImage 不支持 alpha
except ImportError:
    Image = None
    ImageTk = None

# PromptBox 启动日志：失败信息要可见，不要静默
_LOG_DIR = os.environ.get(
    "PROMPTBOX_LOG_DIR",
    os.path.join(os.path.expanduser("~"), ".promptbox"),
)
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _LOG_FILE = os.path.join(_LOG_DIR, "promptbox.log")
except Exception:
    _LOG_FILE = None


def _log(message: str) -> None:
    """记录启动期错误到日志文件，避免 try/except: pass 把问题藏起来。"""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    if _LOG_FILE:
        try:
            with open(_LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass

from promptbox_mvp.ai_service import RepairService
from promptbox_mvp.storage import load_data as load_mvp_data
from promptbox_mvp.storage import save_data as save_mvp_data
from promptbox_mvp.workbench import RepairWorkbench
from promptbox_mvp.prompt_variables import PromptTemplate
from promptbox_mvp.snapshots import append_snapshot, create_snapshot, get_snapshots
from promptbox_mvp.quick_copy import (
    build_quick_copy_run,
    ensure_quick_copy_fields,
    get_selected_version,
    latest_run_times,
    search_prompts,
    version_content,
)
from promptbox_mvp.asset_package import (
    analyze_import,
    apply_import_plan,
    build_import_plan,
    export_asset_package,
    load_import_source,
)
from promptbox_mvp.verification_evidence import (
    VerificationEvidenceError,
    get_version_evidence,
    list_verification_records,
)

# ── 配置 ──────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 6 frozen 状态下 sys._MEIPASS 可能为 None / 缺失 / 缺失 _MEI 前缀。
    # 按以下顺序找 logos/snippets 资源目录，找到第一个含 logos 的就停：
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(os.getcwd())
    for cand in candidates:
        if os.path.isdir(os.path.join(cand, "logos")):
            SCRIPT_DIR = cand
            break
    else:
        SCRIPT_DIR = candidates[0] if candidates else SCRIPT_DIR
DATA_DIR = os.environ.get(
    "PROMPTBOX_DATA_DIR",
    os.path.join(os.path.expanduser("~"), ".promptbox"),
)
os.makedirs(DATA_DIR, exist_ok=True)
SNIPPETS_FILE = os.path.join(DATA_DIR, "snippets.json")
SNIPPETS_DEFAULT = os.path.join(SCRIPT_DIR, "snippets.default.json")
# 旧路径兼容：以前数据存在脚本目录，迁移到 ~/.promptbox/
SNIPPETS_LEGACY = os.path.join(SCRIPT_DIR, "snippets.json")
LEGACY_SNIPPETS_FILE = SNIPPETS_LEGACY

# 演示模式：设置环境变量 PROMPTBOX_DEMO=1 时改读 snippets.demo.json，
# 数据写到 ~/.promptbox/snippets.demo.json，不污染真实数据。
if os.environ.get("PROMPTBOX_DEMO") == "1":
    _demo_src = os.path.join(SCRIPT_DIR, "snippets.demo.json")
    if os.path.exists(_demo_src):
        SNIPPETS_DEFAULT = _demo_src
    SNIPPETS_FILE = os.path.join(DATA_DIR, "snippets.demo.json")
    SNIPPETS_LEGACY = SNIPPETS_FILE  # 关掉 legacy 迁移
    # demo 模式每次强制从模板重建，避免残留
    try:
        if os.path.exists(SNIPPETS_FILE):
            os.remove(SNIPPETS_FILE)
    except Exception:
        pass

ICON_ICO = os.path.join(SCRIPT_DIR, "logos", "promptbox.ico")
ICON_PNG = os.path.join(SCRIPT_DIR, "logos", "icon_256.png")
LOGO_HEADER = os.path.join(SCRIPT_DIR, "logos", "logo_header_36.png")
HOTKEY = "ctrl+shift+space"
# demo 模式换个不常用的热键，避免和真实实例抢占
if os.environ.get("PROMPTBOX_DEMO") == "1":
    HOTKEY = "ctrl+shift+f11"


def apply_window_icon(win):
    """给 tk.Tk / tk.Toplevel 挂上 PromptBox 图标；找不到文件就静默跳过。"""
    try:
        if os.path.exists(ICON_ICO):
            win.iconbitmap(ICON_ICO)
    except Exception as e:
        _log(f"[icon] iconbitmap 失败: {e}")
    try:
        if os.path.exists(ICON_PNG):
            if ImageTk is not None:
                # RGBA 用 ImageTk.PhotoImage
                img = ImageTk.PhotoImage(Image.open(ICON_PNG))
            else:
                # Pillow 不可用时降级为 tk.PhotoImage（仅支持 RGB）
                img = tk.PhotoImage(file=ICON_PNG)
            win._pb_icon_ref = img
            win.iconphoto(True, img)
    except Exception as e:
        _log(f"[icon] iconphoto 失败: {e}")


def _load_logo_image(path: str, size: tuple[int, int] | None = None):
    """加载带 alpha 通道的 logo PNG，失败时返回 None 并写日志。"""
    if not os.path.exists(path):
        _log(f"[logo] 文件不存在: {path}")
        return None
    if ImageTk is not None:
        try:
            img = Image.open(path)
            if size is not None:
                img = img.resize(size, Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception as e:
            _log(f"[logo] Pillow 加载失败 {path}: {e}")
    try:
        # Pillow 不可用时尝试 tk.PhotoImage（仅支持 RGB；RGBA 会抛错）
        return tk.PhotoImage(file=path)
    except Exception as e:
        _log(f"[logo] tk.PhotoImage 加载失败 {path}（多半是 RGBA 无 Pillow 兜底）: {e}")
        return None

# ── UI ────────────────────────────────────────────────────
FONT = "Microsoft YaHei UI"
THEMES = {
    "cafe": {
        "name": "暖心咖啡 (Cozy Café)",
        "bg": "#faf6ee", "bg_input": "#f4ede2", "bg_panel": "#efe5d3", "bg_hover": "#e4d9c5",
        "fg": "#4a3c31", "fg_dim": "#8c7a6b", "accent": "#b38a5f", "accent_2": "#8a9a86",
        "warn": "#d97706", "danger": "#c94a29", "border": "#dfd5c2", "border_sec": "#d8cdb8",
        "btn_secondary": "#e7dcc4", "btn_secondary_fg": "#3a2f25", "btn_danger": "#c94a29", "btn_danger_fg": "#fff8eb"
    },
    "cyber": {
        "name": "赛博霓虹 (Cyberpunk Neon)",
        "bg": "#0d0c15", "bg_input": "#1a192e", "bg_panel": "#141323", "bg_hover": "#252342",
        "fg": "#e0def4", "fg_dim": "#908caa", "accent": "#ff007f", "accent_2": "#00f0ff",
        "warn": "#f6c177", "danger": "#eb6f92", "border": "#2a283e", "border_sec": "#3b3857",
        "btn_secondary": "#252342", "btn_secondary_fg": "#e0def4", "btn_danger": "#eb6f92", "btn_danger_fg": "#0d0c15"
    }
}
CURRENT_THEME = "cafe"
T = THEMES[CURRENT_THEME]

BG = T["bg"]
BG_INPUT = T["bg_input"]
BG_PANEL = T["bg_panel"]
BG_HOVER = T["bg_hover"]
FG = T["fg"]
FG_DIM = T["fg_dim"]
ACCENT = T["accent"]
ACCENT_2 = T["accent_2"]
WARN = T["warn"]
DANGER = T["danger"]
BORDER = T["border"]
BTN_SECONDARY = T.get("btn_secondary", BG_INPUT)
BTN_SECONDARY_FG = T.get("btn_secondary_fg", FG)
BTN_DANGER = T.get("btn_danger", DANGER)
BTN_DANGER_FG = T.get("btn_danger_fg", BG)

WIN_W, WIN_H = 980, 620
LEFT_W = 260

DEFAULT_CATEGORY_ID = "cat_inbox"
DEFAULT_CATEGORY_NAME = "未分类"

DEFAULT_TAGS = [
    "技能优化", "文档输入", "GitHub输入", "外部资料", "本地技能", "经验抽象",
    "去AI味", "写作", "排版", "调研", "质检", "知识库", "代码", "日常"
]

DEFAULT_CATEGORIES = [
    {"id": "cat_skill", "name": "技能优化", "parent_id": None, "children": ["cat_skill_doc", "cat_skill_github"]},
    {"id": "cat_skill_doc", "name": "文档驱动", "parent_id": "cat_skill", "children": []},
    {"id": "cat_skill_github", "name": "GitHub驱动", "parent_id": "cat_skill", "children": []},
    {"id": "cat_writing", "name": "写作辅助", "parent_id": None, "children": ["cat_writing_inspect"]},
    {"id": "cat_writing_inspect", "name": "文字质检", "parent_id": "cat_writing", "children": []},
    {"id": DEFAULT_CATEGORY_ID, "name": DEFAULT_CATEGORY_NAME, "parent_id": None, "children": []},
]

TAG_KEYWORDS = {
    "技能优化": ["skill", "技能", "体系", "迭代", "优化", "流程", "步骤"],
    "文档输入": ["文档", "pdf", "docx", "外部文档", "资料", "文件"],
    "GitHub输入": ["github", "repo", "仓库", "开源", "外部技能"],
    "外部资料": ["外部", "资料", "案例", "专业性", "输入"],
    "本地技能": ["本地", "已有", "现有", "共存", "迁移"],
    "经验抽象": ["抽象", "通用", "经验", "规则", "复用"],
    "去AI味": ["ai味", "机械", "humanizer", "去ai", "质感"],
    "写作": ["写", "文章", "文本", "表达", "段落"],
    "排版": ["排版", "html", "公众号", "样式"],
    "调研": ["调研", "搜索", "扫描", "热点", "研究"],
    "质检": ["质检", "检查", "审查", "review", "评分"],
    "知识库": ["知识库", "wiki", "quote", "index", "log"],
    "代码": ["代码", "实现", "bug", "测试", "函数"],
    "日常": ["日常", "备忘", "提醒", "整理"],
}

# ── 数据 ──────────────────────────────────────────────────

# 版本状态常量
VER_DRAFT = "draft"
VER_STABLE = "stable"
VER_ARCHIVED = "archived"


def now():
    return datetime.now().isoformat()


def make_version_id():
    return make_id("ver", str(int(datetime.now().timestamp() * 1_000_000)))


def _unconfigured_repair_transport(_messages):
    raise ValueError(
        "未配置修复服务。设置 PROMPTBOX_REPAIR_API_BASE 和 PROMPTBOX_REPAIR_API_KEY 后重启 PromptBox。"
    )


def build_openai_compatible_transport():
    """Return an OpenAI-compatible transport configured by environment variables."""
    api_base = os.environ.get("PROMPTBOX_REPAIR_API_BASE", "").strip().rstrip("/")
    api_key = os.environ.get("PROMPTBOX_REPAIR_API_KEY", "").strip()
    model = os.environ.get("PROMPTBOX_REPAIR_MODEL", "").strip()
    if not api_base or not api_key or not model:
        return _unconfigured_repair_transport

    def transport(messages):
        payload = json.dumps({"model": model, "messages": messages}).encode("utf-8")
        request = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ValueError("修复服务请求失败") from exc
        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("修复服务返回了无效响应") from exc

    return transport


def version_evidence_action(snippet, version_id):
    """Return the read-only evidence action for versions linked to a repair case."""
    version = next(
        (item for item in snippet.get("versions", []) if item.get("id") == version_id),
        None,
    )
    if not version or not version.get("repair_case_id"):
        return None
    return "view_evidence"


def prompt_display_content(snippet):
    """完整工作台兼容逻辑：优先显示稳定版，缺失时显示当前版。"""
    stable_id = snippet.get("stable_version_id")
    versions = snippet.get("versions", [])
    by_id = {v["id"]: v for v in versions}
    if stable_id and stable_id in by_id:
        return by_id[stable_id]["content"]
    for v in versions:
        if v["id"] == snippet.get("current_version_id"):
            return v["content"]
    return snippet.get("content", "")


def get_prompt_version_content(snippet, version_id=None):
    """V3 读取明确版本；缺省只读取当前版本，不隐式回退稳定版。"""
    return version_content(snippet, version_id)


def snippet_version_label(snippet):
    """返回版本标记文字，如 'v3·稳定' 或 'v1'"""
    versions = snippet.get("versions", [])
    by_id = {v["id"]: v for v in versions}
    cur_id = snippet.get("current_version_id")
    stable_id = snippet.get("stable_version_id")
    cur_v = by_id.get(cur_id, {}) if cur_id else {}
    vnum = cur_v.get("version_number", 1)
    if stable_id and cur_id and stable_id == cur_id:
        return f"v{vnum}·稳定"
    return f"v{vnum}"


def snippet_stable_content(snippet):
    """获取稳定版内容，无稳定版返回 None"""
    stable_id = snippet.get("stable_version_id")
    if not stable_id:
        return None
    for v in snippet.get("versions", []):
        if v["id"] == stable_id:
            return v["content"]
    return None


def make_id(prefix, text=""):
    base = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", text.strip()).strip("_").lower()
    if not base:
        base = datetime.now().strftime("%Y%m%d%H%M%S%f") + "_" + str(int(datetime.now().timestamp() * 1_000_000) % 1_000_000)
    return f"{prefix}_{base}"


def extract_placeholders(text):
    return PromptTemplate.from_text(text).variable_names


def fill_placeholders(text, mapping):
    return PromptTemplate.from_text(text).render(mapping)


def build_default_data():
    return {
        "version": 4,
        "categories": [dict(c) for c in DEFAULT_CATEGORIES],
        "tags": [{"id": make_id("tag", name), "name": name} for name in DEFAULT_TAGS],
        "snippets": [],
        "runs": [],
        "repair_cases": [],
        "preferences": {},
    }


def ensure_category_links(categories):
    by_id = {c["id"]: c for c in categories}
    for c in categories:
        c.setdefault("parent_id", None)
        c["children"] = []
    for c in categories:
        pid = c.get("parent_id")
        if pid in by_id:
            by_id[pid].setdefault("children", [])
            if c["id"] not in by_id[pid]["children"]:
                by_id[pid]["children"].append(c["id"])
    if DEFAULT_CATEGORY_ID not in by_id:
        categories.append({"id": DEFAULT_CATEGORY_ID, "name": DEFAULT_CATEGORY_NAME, "parent_id": None, "children": []})
    return categories


def normalize_prompt_data(raw):
    if isinstance(raw, dict) and "snippets" in raw:
        data = raw
        data.setdefault("version", 4)
        data.setdefault("categories", [])
        data.setdefault("tags", [])
        data.setdefault("snippets", [])
        data.setdefault("runs", [])
        data.setdefault("repair_cases", [])
        data.setdefault("preferences", {})
    else:
        data = build_default_data()
        data["snippets"] = raw if isinstance(raw, list) else []

    if not data.get("categories"):
        data["categories"] = [dict(c) for c in DEFAULT_CATEGORIES]
    data["categories"] = ensure_category_links(data["categories"])

    tag_by_name = {t.get("name"): t for t in data.get("tags", []) if t.get("name")}
    for name in DEFAULT_TAGS:
        if name not in tag_by_name:
            tag_by_name[name] = {"id": make_id("tag", name), "name": name}
    data["tags"] = list(tag_by_name.values())

    # ── v2→v3 迁移：为每个 snippet 添加版本/变体/来源字段 ──
    changed_snippets = []
    for idx, s in enumerate(data.get("snippets", [])):
        s.setdefault("id", make_id("snip", s.get("title", str(idx))))
        s.setdefault("title", "未命名提示词")
        s.setdefault("created_at", now())
        s.setdefault("updated_at", s["created_at"])
        s.setdefault("category_id", DEFAULT_CATEGORY_ID)

        # v3 新增字段
        s.setdefault("source_prompt_id", None)
        s.setdefault("source_version_id", None)
        s.setdefault("scenario", "")
        s.setdefault("_deleted", False)
        s.setdefault("is_favorite", False)

        # 版本迁移：如果还没有 versions，把旧 content 包装成 v1
        if "versions" not in s or not isinstance(s.get("versions"), list):
            old_content = s.pop("content", "")
            v1_id = make_version_id()
            s["versions"] = [{
                "id": v1_id,
                "version_number": 1,
                "content": old_content,
                "changelog": "初始版本",
                "status": VER_STABLE,
                "created_at": s.get("created_at", now()),
                "parent_version_id": None,
                "repair_case_id": None,
            }]
            s["current_version_id"] = v1_id
            s["stable_version_id"] = v1_id
        else:
            s.setdefault("current_version_id", s["versions"][-1]["id"] if s["versions"] else None)
            s.setdefault("stable_version_id", None)
            for v in s.get("versions", []):
                v.setdefault("id", make_version_id())
                v.setdefault("version_number", 1)
                v.setdefault("status", VER_DRAFT)
                v.setdefault("changelog", "")
                v.setdefault("parent_version_id", None)
                v.setdefault("repair_case_id", None)

        # s["content"] 已由版本接管，但保留兼容访问
        s.setdefault("content", "")
        if not isinstance(s.get("variable_definitions"), dict):
            s["variable_definitions"] = {}
        if not isinstance(s.get("snapshots"), list):
            s["snapshots"] = []

        if "tag_ids" not in s:
            tag_ids = []
            for tag_name in s.get("tags", []):
                tag_name = str(tag_name).strip()
                if not tag_name:
                    continue
                if tag_name not in tag_by_name:
                    tag_by_name[tag_name] = {"id": make_id("tag", tag_name), "name": tag_name}
                tag_ids.append(tag_by_name[tag_name]["id"])
            s["tag_ids"] = list(dict.fromkeys(tag_ids))
        s.pop("tags", None)
        changed_snippets.append(s)
    data["snippets"] = ensure_quick_copy_fields(changed_snippets)
    data["tags"] = list(tag_by_name.values())
    data["version"] = 4
    data.setdefault("repair_cases", [])
    data.setdefault("preferences", {})
    return data


def load_prompt_data():
    source_path = SNIPPETS_FILE
    if not os.path.exists(source_path) and os.path.exists(LEGACY_SNIPPETS_FILE):
        source_path = LEGACY_SNIPPETS_FILE
    data = normalize_prompt_data(load_mvp_data(source_path))
    save_prompt_data(data)
    return data


def save_prompt_data(data):
    data = normalize_prompt_data(data)
    data["version"] = 4
    save_mvp_data(SNIPPETS_FILE, data)


def load_snippets():
    """兼容旧测试和外部脚本：只返回 snippets 列表。"""
    return load_prompt_data()["snippets"]


def save_snippets(snippets):
    data = load_prompt_data()
    data["snippets"] = snippets
    save_prompt_data(data)


def tag_name_to_id(data):
    return {t["name"]: t["id"] for t in data.get("tags", [])}


def tag_id_to_name(data):
    return {t["id"]: t["name"] for t in data.get("tags", [])}


def category_name(data, category_id):
    for c in data.get("categories", []):
        if c["id"] == category_id:
            return c["name"]
    return DEFAULT_CATEGORY_NAME


def collect_category_descendants(data, category_id):
    children = {c["id"]: c.get("children", []) for c in data.get("categories", [])}
    result = {category_id}
    stack = list(children.get(category_id, []))
    while stack:
        cid = stack.pop()
        result.add(cid)
        stack.extend(children.get(cid, []))
    return result


def build_smart_tag_prompt(title, content, existing_tags, limit=5):
    tag_line = "、".join(existing_tags) if existing_tags else "无"
    return f"""你是 PromptBox 的提示词整理助手。请为一个提示词推荐 {limit} 个以内的短标签。

规则：
1. 优先从“已有标签池”中选择，方便用户横向管理。
2. 只有已有标签无法覆盖核心用途时，才新增标签。
3. 标签必须短、扁平、可复用；不要做层级标签，不要写逗号分隔给用户手填。
4. 输出 JSON 数组字符串，例如 [\"技能优化\", \"文档输入\"]。

已有标签池：{tag_line}
标题：{title}
内容：{content[:1200]}"""


def recommend_tags(title, content, existing_tags, limit=5):
    """标签推荐：优先复用已有标签；无匹配时生成少量新标签。

    这里保留清晰的 LLM prompt 构造函数；桌面小工具本身不绑定外部 API，
    因此默认用可解释的关键词评分做本地推荐，后续可把 build_smart_tag_prompt
    交给 WorkBuddy/任意 LLM 后替换本函数返回值。
    """
    text = f"{title}\n{content}".lower()
    scored = []
    for tag in existing_tags:
        name = str(tag).strip()
        if not name:
            continue
        score = 0
        if name.lower() in text:
            score += 5
        for kw in TAG_KEYWORDS.get(name, []):
            if kw.lower() in text:
                score += 2
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    result = [name for _, name in scored[:limit]]

    if len(result) >= min(limit, 2) or not existing_tags:
        generated = []
        if any(w in text for w in ["文档", "资料", "pdf", "docx"]):
            generated.append("文档输入")
        if any(w in text for w in ["github", "仓库", "repo"]):
            generated.append("GitHub输入")
        if any(w in text for w in ["技能", "skill", "体系"]):
            generated.append("技能优化")
        if any(w in text for w in ["抽象", "通用", "复用", "经验"]):
            generated.append("经验抽象")
        if not generated:
            words = re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{3,12}", title + " " + content)
            generated = [w[:6] for w in words[:limit]] or ["待整理"]
        for name in generated:
            if name not in result:
                result.append(name)
            if len(result) >= limit:
                break
    return result[:limit]


def kill_existing_instances():
    """强制终止已存在的其他 python.exe 或 pythonw.exe 的 PromptBox 后台实例，防止多实例并发和热键被占用。"""
    my_pid = os.getpid()
    try:
        import psutil
    except ImportError:
        # 如果没有安装 psutil，使用 subprocess 调用系统命令查询与终止
        import subprocess
        try:
            # 仅在 Windows 环境下做清理，且排除自身 PID
            # wmic CSV 格式：Node,ProcessId,CommandLine
            cmd = 'wmic process where "name=\'python.exe\' or name=\'pythonw.exe\'" get ProcessId, CommandLine /format:csv'
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith("Node"):
                    continue
                if 'promptbox' not in line.lower():
                    continue
                parts = line.split(',')
                try:
                    # wmic CSV: Node(~0), ProcessId(~1), CommandLine(~2+)
                    pid = int(parts[1].strip())
                    if pid != my_pid:
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
                except (ValueError, IndexError):
                    pass
        except Exception:
            pass
        return

    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = proc.info['pid']
                if pid == my_pid:
                    continue
                name = (proc.info['name'] or '').lower()
                if 'python' not in name:
                    continue
                cmdline = proc.info['cmdline'] or []
                full = ' '.join(cmdline).lower()
                if 'promptbox' in full:
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass


def copy_to_clipboard(text):
    """Copy text and return success; callers must not record failed copies."""
    if pyperclip is None:
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception as exc:
        _log(f"[clipboard] 复制失败: {exc}")
        return False


def do_paste(text):
    old = None
    try:
        old = pyperclip.paste()
    except Exception:
        pass
    copy_to_clipboard(text)

    def _do():
        try:
            import time
            # 物理键释放等待与状态清理，防止热键粘滞
            for key in ["ctrl", "shift", "alt", "space", "win"]:
                try:
                    keyboard.release(key)
                except Exception:
                    pass
            
            # 稍微多等一会儿，确保 Tkinter 窗口完全关闭，且系统焦点完全回到之前的应用中
            time.sleep(0.2)
            
            # 再次确认释放
            for key in ["ctrl", "shift", "alt", "space"]:
                try:
                    keyboard.release(key)
                except Exception:
                    pass
                    
            # 发送粘贴快捷键
            keyboard.send("ctrl+v")
            
            # 再次释放 ctrl，防止 ctrl 键粘滞
            time.sleep(0.05)
            try:
                keyboard.release("ctrl")
            except Exception:
                pass
                
            time.sleep(0.2)
        finally:
            if old is not None:
                try:
                    pyperclip.copy(old)
                except Exception:
                    pass

    threading.Thread(target=_do, daemon=True).start()


# ── 主应用 ────────────────────────────────────────────────
SORT_OPTIONS = [
    ("updated_at", "最近编辑", True),
    ("created_at", "创建时间", True),
    ("title", "标题", False),
]


class PromptBox:
    def __init__(self, repair_transport=None):
        self.data = build_default_data()
        self.snippets = []
        self.repair_transport = repair_transport or build_openai_compatible_transport()
        self.repair_service = RepairService(self.repair_transport)
        self.filtered = []
        self.selected = None
        self.ph_entries = {}
        self.ph_definitions = {}
        self.sort_idx = 0
        self.active_tags = set()
        self.active_category_id = None
        self.win = None
        self.root = None
        self._opening = False
        self._drag_snippet = None
        self._drag_start = None
        self._drag_active = False
        self._drag_preview = None
        self.substructure_frame = None
        self.sb_tree = None
        self._mode = "list"
        self.palette_win = None
        self.palette_selected = None
        self.palette_selected_version_id = None
        self.palette_sort_mode = "recent"
        self.palette_variable_entries = {}
        self._last_quick_copy_signature = None
        self._last_quick_copy_at = 0.0

    def toggle(self):
        if self._opening:
            return
        if self.win and self.win.winfo_exists():
            self.win.destroy()
            self.win = None
            return
        # 激活前，稍微释放下修饰键，防止热键粘滞
        for key in ["ctrl", "shift", "alt", "space"]:
            try:
                keyboard.release(key)
            except Exception:
                pass
        self._opening = True
        try:
            self._open()
        finally:
            self._opening = False

    def _open(self):
        self.data = load_prompt_data()
        self.snippets = self.data["snippets"]
        # 保留“全部提示词”状态；不要在打开窗口时偷偷选中第一个分类。
        # 这样用户才能明确取消分类筛选，并在全部列表里新建提示词。
        self._restore_category_selection()

        self.win = tk.Toplevel(self.root)
        self.win.title("PromptBox")
        apply_window_icon(self.win)
        self.win.lift()
        self.win.focus_force()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{WIN_W}x{WIN_H}+{(sw-WIN_W)//2}+{(sh-WIN_H)//2}")
        self.win.configure(bg=BG)
        self.win.protocol("WM_DELETE_WINDOW", self._close)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("PB.Treeview", background=BG_INPUT, foreground=FG,
                        fieldbackground=BG_INPUT, font=(FONT, 12), rowheight=34, borderwidth=0)
        style.configure("PB.Treeview.Heading", background=BG_PANEL, foreground=FG,
                        font=(FONT, 12, "bold"), borderwidth=0)
        style.map("PB.Treeview", background=[("selected", ACCENT)], foreground=[("selected", BG)])

        header = tk.Frame(self.win, bg=BG)
        header.pack(fill="x", padx=16, pady=(12, 6))
        # Logo + 标题。tk.PhotoImage 不支持 RGBA，必须用 Pillow 的 ImageTk.PhotoImage
        self._logo_img = _load_logo_image(LOGO_HEADER)
        if self._logo_img is not None:
            tk.Label(header, image=self._logo_img, bg=BG).pack(side="left", padx=(0, 8))
        # 标题始终显示，logo 加载失败时仅显示文字
        tk.Label(header, text="PromptBox", bg=BG, fg=ACCENT, font=(FONT, 18, "bold")).pack(side="left")
        self.count_label = tk.Label(header, text="", bg=BG, fg=FG_DIM, font=(FONT, 11))
        self.count_label.pack(side="left", padx=(12, 0))
        self.sort_label = tk.Label(header, text="", bg=BG, fg=FG_DIM, font=(FONT, 10), cursor="hand2")
        self.sort_label.pack(side="right")
        self.sort_label.bind("<Button-1>", lambda e: self._cycle_sort())
        self._update_sort_label()

        self.theme_label = tk.Label(header, text=f"🎨 {T['name'].split()[0]}", bg=BG, fg=ACCENT_2, font=(FONT, 10), cursor="hand2")
        self.theme_label.pack(side="right", padx=(0, 16))
        self.theme_label.bind("<Button-1>", lambda e: self._cycle_theme())

        sf = tk.Frame(self.win, bg=BG)
        sf.pack(fill="x", padx=16, pady=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter())
        self.search_entry = tk.Entry(sf, textvariable=self.search_var, bg=BG_INPUT, fg=FG,
                                     insertbackground=FG, relief="flat", font=(FONT, 14), bd=0,
                                     highlightthickness=0,
                                     selectbackground=ACCENT, selectforeground=BG)
        self.search_entry.pack(fill="x", expand=True, ipady=8)

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        left = tk.Frame(body, bg=BG_PANEL, width=LEFT_W)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)
        left_header = tk.Frame(left, bg=BG_PANEL)
        left_header.pack(fill="x", padx=8, pady=8)
        tk.Label(left_header, text="分类", bg=BG_PANEL, fg=FG, font=(FONT, 12, "bold")).pack(side="left")
        self._new_cat_btn = self._btn(left_header, "＋ 新分类", self._popup_new_category_menu, BTN_SECONDARY, BTN_SECONDARY_FG, 10)
        self._new_cat_btn.pack(side="right")
        self._all_prompts_btn = self._btn(
            left,
            self._category_clear_label(),
            self._deselect_category,
            ACCENT if self.active_category_id is None else BTN_SECONDARY,
            BG if self.active_category_id is None else BTN_SECONDARY_FG,
            10,
        )
        self._all_prompts_btn.pack(fill="x", padx=8, pady=(0, 6))
        self.cat_tree = ttk.Treeview(left, show="tree", style="PB.Treeview", selectmode="browse")
        self.cat_tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.cat_tree.bind("<<TreeviewSelect>>", lambda e: self._on_category_select())
        self.cat_tree.bind("<Button-1>", self._on_category_click)
        self.cat_tree.bind("<Button-3>", self._category_menu)

        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        # 两种视图：list（默认，父分类也走它 + 顶部 chip 条）和 tagview（标签焦点）
        # 顶部：子分类 chip 跳转条（父分类点击时显示）
        self.subcat_bar = tk.Frame(right, bg=BG)
        # 列表容器
        self.list_container = tk.Frame(right, bg=BG)
        self.tree = ttk.Treeview(self.list_container, columns=("t", "p"), show="headings", style="PB.Treeview")
        self.tree.heading("t", text="  标题")
        self.tree.heading("p", text="  预览")
        self.tree.column("t", width=260, anchor="w")
        self.tree.column("p", width=430, anchor="w")
        self.sb_tree = ttk.Scrollbar(self.list_container, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.sb_tree.set)
        # substructure_frame 保留但不再使用（deprecated，防外部引用崩溃）
        self.substructure_frame = tk.Frame(right, bg=BG)
        self.tag_view_frame = tk.Frame(right, bg=BG)
        self._mode = "list"
        self.list_container.pack(fill="both", expand=True)
        self.tree.pack(side="left", fill="both", expand=True)
        self.sb_tree.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.tree.bind("<Double-1>", lambda e: self._on_double())
        self.tree.bind("<Return>", lambda e: self._on_enter())
        self.tree.bind("<Button-3>", self._snippet_menu)
        # 拖曳：条目拖到左侧分类树
        self.tree.bind("<ButtonPress-1>", self._on_drag_start, add="+")
        # Motion/Release 绑在主窗口上以保证跨控件追踪
        self.win.bind("<B1-Motion>", self._on_drag_motion, add="+")
        self.win.bind("<ButtonRelease-1>", self._on_drag_release, add="+")
        self.win.bind("<Escape>", lambda e: self._cancel_drag(), add="+")

        self.tags_frame = tk.Frame(self.win, bg=BG)
        self.tags_frame.pack(fill="x", padx=16, pady=(0, 6))

        self.action_frame = tk.Frame(self.win, bg=BG)
        self.action_frame.pack(fill="x", padx=16, pady=(0, 6))
        self.ph_frame = tk.Frame(self.win, bg=BG)
        self.ph_frame.pack(fill="x", padx=16, pady=(0, 6))

        bf = tk.Frame(self.win, bg=BG)
        bf.pack(fill="x", padx=16, pady=(0, 12))
        self._build_footer_actions(bf)

        self.search_entry.bind("<Up>", lambda e: self._move(-1))
        self.search_entry.bind("<Down>", lambda e: self._move(1))
        self.search_entry.bind("<Return>", lambda e: self._on_enter())
        self.search_entry.bind("<Escape>", lambda e: self._close())
        
        # 关键修改：为了防止非特定按键事件误触发和按键绑定污染输入框
        # 将全局绑定限制为焦点不在文本输入控件中时才生效，或者通过精准解绑/重绑定
        # 同时防止全局 Esc/q/e/d 误触发
        def global_key(event, action):
            # 只有当焦点不在输入框（Entry/Text）时，或者快捷键匹配时，才执行全局响应
            f = self.win.focus_get()
            if isinstance(f, (tk.Entry, tk.Text)):
                # 如果当前聚焦的是输入框，则绝大多数全局单字母快捷键（比如 e, d, q）应当被当作普通字符输入，禁止触发编辑/删除/退出
                return
            action()

        self.win.bind("<Escape>", lambda e: self._close())
        self.win.bind("<Up>", lambda e: global_key(e, lambda: self._move(-1)))
        self.win.bind("<Down>", lambda e: global_key(e, lambda: self._move(1)))
        self.win.bind("<e>", lambda e: global_key(e, self._edit))
        self.win.bind("<d>", lambda e: global_key(e, self._delete))
        self.win.bind("<q>", lambda e: global_key(e, self._quit_app))

        self._render_categories()
        self._update_tags()
        self._filter()
        self.search_entry.focus_set()

    def _get_verification_records(self, **filters):
        """Return flattened verification runs for the read-only browser."""
        provider = getattr(self, "_verification_records", None)
        if callable(provider):
            return provider(**filters)
        return list_verification_records(self.data, **filters)

    def _build_footer_actions(self, parent):
        tk.Label(parent, text="↑↓ 选择 · Enter 展开/复制 · E 编辑 · D 删除 · Esc 隐藏 · Q 退出",
                 bg=BG, fg=FG_DIM, font=(FONT, 10)).pack(side="left")
        self._btn(parent, "＋ 新建", self._add, BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right")
        self._btn(parent, "导入资产包", self._import_asset_package, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="right", padx=(0, 6))
        self._btn(parent, "导出资产包", self._export_asset_package, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="right", padx=(0, 6))
        self._btn(parent, "验证记录", self._show_verification_records, BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=(0, 6))
        self._btn(parent, "调优提示词", self._open_repair_workbench, ACCENT, BG, 11).pack(side="right", padx=(0, 6))
        self._btn(parent, "退出", self._quit_app, BTN_DANGER, BTN_DANGER_FG, 10).pack(side="right", padx=(0, 6))

    def _export_asset_package(self):
        """Export selected Prompt assets as Markdown package."""
        if self.win is None:
            raise RuntimeError("PromptBox window is unavailable")
        from tkinter import filedialog
        dialog = tk.Toplevel(self.win)
        dialog.title("导出资产包")
        apply_window_icon(dialog)
        dialog.configure(bg=BG)
        dialog.geometry("520x330")
        dialog.transient(self.win)
        dialog.grab_set()
        tk.Label(dialog, text="把提示词资产导出为可读 Markdown 文件", bg=BG, fg=FG,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=20, pady=(18, 14))
        scope_var = tk.StringVar(value="all")
        scope_frame = tk.Frame(dialog, bg=BG)
        scope_frame.pack(fill="x", padx=20)
        scopes = [("all", "全部未删除提示词"), ("category", "当前分类"), ("filtered", "当前筛选结果"), ("selected", "勾选项"), ("single", "当前选中")]
        for value, label in scopes:
            tk.Radiobutton(scope_frame, text=label, variable=scope_var, value=value,
                           bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                           selectcolor=BG_INPUT, font=(FONT, 10)).pack(anchor="w", pady=2)
        zip_var = tk.BooleanVar(value=False)
        tk.Checkbutton(dialog, text="同时生成 ZIP 压缩包", variable=zip_var,
                       bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                       selectcolor=BG_INPUT, font=(FONT, 10)).pack(anchor="w", padx=20, pady=(10, 4))
        tk.Label(dialog, text="导出目录会自动创建新文件夹，不覆盖已有资产包。", bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).pack(anchor="w", padx=20, pady=(0, 12))
        button_row = tk.Frame(dialog, bg=BG)
        button_row.pack(fill="x", padx=20, side="bottom", pady=16)
        def do_export():
            parent = filedialog.askdirectory(parent=dialog, title="选择资产包保存位置")
            if not parent:
                return
            scope = scope_var.get()
            if scope == "single" and not self.selected:
                messagebox.showwarning("无法导出", "请先选择一个提示词。", parent=dialog)
                return
            if scope == "selected":
                selected_ids = {s.get("id") for s in self.filtered if s.get("id")}
            else:
                selected_ids = None
            try:
                result = export_asset_package(
                    self.snippets, parent,
                    category_names={c.get("id"): c.get("name") for c in self.data.get("categories", [])},
                    tag_names=tag_id_to_name(self.data), include_zip=zip_var.get(),
                    scope=scope, category=self.active_category_id, selected_ids=selected_ids,
                    filtered_snippets=self.filtered, snippet_id=(self.selected or {}).get("id"),
                )
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc), parent=dialog)
                return
            dialog.destroy()
            suffix = f"\nZIP：{result.zip_path}" if result.zip_path else ""
            messagebox.showinfo("导出完成", f"已导出 {len(result.manifest.get('items', []))} 条提示词。\n目录：{result.directory}{suffix}", parent=self.win)
        self._btn(button_row, "取消", dialog.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="right")
        self._btn(button_row, "导出", do_export, ACCENT, BG, 10).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda e: dialog.destroy())

    def _import_asset_package(self):
        """Preview Markdown package and ask user to select changes."""
        if self.win is None:
            raise RuntimeError("PromptBox window is unavailable")
        from tkinter import filedialog
        source = filedialog.askopenfilename(parent=self.win, title="选择资产包文件夹中的 manifest.json 或 ZIP", filetypes=[("资产包 ZIP", "*.zip"), ("Manifest", "manifest.json"), ("全部文件", "*.*")])
        if source and Path(source).name.lower() == "manifest.json":
            source = str(Path(source).parent)
        if not source:
            source = filedialog.askdirectory(parent=self.win, title="选择资产包文件夹")
        if not source:
            return
        try:
            with load_import_source(source) as root:
                preview = analyze_import(root, self.snippets)
                self._show_import_preview(preview, source)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc), parent=self.win)

    def _show_import_preview(self, preview, source):
        dialog = tk.Toplevel(self.win)
        dialog.title("导入资产包 - 逐条预览")
        apply_window_icon(dialog)
        dialog.configure(bg=BG)
        dialog.geometry("900x560")
        dialog.transient(self.win)
        dialog.grab_set()
        tk.Label(dialog, text="逐条选择要应用的变化；未变化和非法项不会写回。", bg=BG, fg=FG_DIM,
                 font=(FONT, 10)).pack(anchor="w", padx=18, pady=(14, 8))
        frame = tk.Frame(dialog, bg=BG)
        frame.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        tree = ttk.Treeview(frame, columns=("apply", "status", "title", "path", "reason"), show="headings", style="PB.Treeview")
        for col, label, width in [("apply", "应用", 55), ("status", "状态", 120), ("title", "标题", 180), ("path", "路径", 260), ("reason", "说明", 250)]:
            tree.heading(col, text=label); tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set); tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        states = {}
        status_names = {"new": "新增", "updated": "有变化", "unchanged": "未变化", "same_name_different_id": "同名不同 ID", "deleted_id_conflict": "已删除冲突", "invalid": "非法", "unregistered": "未登记"}
        for item in preview.items:
            can_apply = item.selectable and item.status not in {"unchanged", "invalid", "unregistered"}
            states[item.key] = can_apply
            tree.insert("", "end", iid=item.key, values=("☑" if can_apply else "☐", status_names.get(item.status, item.status), item.title or "（无标题）", item.path, item.reason))
        def toggle(_event=None):
            selection = tree.selection()
            if not selection:
                return
            key = selection[0]
            item = next((x for x in preview.items if x.key == key), None)
            if not item or not item.selectable or item.status in {"unchanged", "invalid", "unregistered"}:
                return
            states[key] = not states[key]
            values = list(tree.item(key, "values")); values[0] = "☑" if states[key] else "☐"; tree.item(key, values=values)
        tree.bind("<Double-1>", toggle)
        tree.bind("<space>", toggle)
        button_row = tk.Frame(dialog, bg=BG); button_row.pack(fill="x", padx=18, pady=(0, 14))
        def apply_selected():
            selected = {key for key, enabled in states.items() if enabled}
            if not selected:
                messagebox.showinfo("导入资产包", "没有选择可应用的变化。", parent=dialog); return
            if not messagebox.askyesno("确认写回", f"将应用 {len(selected)} 条变化，并保留原版本。继续？", parent=dialog):
                return
            try:
                plan = build_import_plan(preview, selected, self.data)
                new_data = apply_import_plan(self.data, plan)
                save_prompt_data(new_data)
                self.data = load_prompt_data(); self.snippets = self.data["snippets"]
                self._render_categories(); self._filter(); dialog.destroy()
                counts = {}
                for item in preview.items:
                    counts[item.status] = counts.get(item.status, 0) + 1
                report_lines = [
                    f"来源：{source}",
                    f"发现 {len(preview.items)} 条，应用 {len(selected)} 条，跳过 {len(preview.items) - len(selected)} 条。",
                    "状态统计：" + "、".join(f"{status} {count}" for status, count in counts.items()),
                    "原版本已保留；未变化、非法和未登记项未写回。",
                ]
                if preview.errors:
                    report_lines.append("错误：" + "；".join(preview.errors))
                messagebox.showinfo("导入结果报告", "\n".join(report_lines), parent=self.win)
            except Exception as exc:
                messagebox.showerror("导入失败", str(exc), parent=dialog)
        self._btn(button_row, "关闭", dialog.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="right")
        self._btn(button_row, "应用选中项", apply_selected, ACCENT, BG, 11).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda e: dialog.destroy())

    def _apply_import_asset_package(self):
        """Compatibility hook for external callers; preview owns apply action."""
        return None

    def _show_verification_records(self):
        if self.win is None:
            raise RuntimeError("PromptBox window is unavailable")
        dlg = tk.Toplevel(self.win)
        dlg.title("验证记录")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("920x620")
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        tk.Label(dlg, text="只读查看：这里展示已经保存的验证案例，不修改提示词或版本。",
                 bg=BG, fg=FG_DIM, font=(FONT, 10)).pack(anchor="w", padx=18, pady=(14, 8))

        filter_row = tk.Frame(dlg, bg=BG)
        filter_row.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(filter_row, text="人工裁决", bg=BG, fg=FG, font=(FONT, 10)).pack(side="left")
        verdict_var = tk.StringVar(value="全部")
        verdict_values = {
            "全部": None,
            "候选更好": "candidate_better",
            "基线更好": "baseline_better",
            "相同": "equal",
            "未决定": "undecided",
        }
        verdict_menu = tk.OptionMenu(filter_row, verdict_var, *verdict_values.keys())
        verdict_menu.configure(bg=BG_INPUT, fg=FG, activebackground=BG_HOVER, activeforeground=FG,
                               relief="flat", bd=0, font=(FONT, 10))
        verdict_menu["menu"].configure(bg=BG_PANEL, fg=FG)
        verdict_menu.pack(side="left", padx=(8, 18))
        tk.Label(filter_row, text="上下文标签", bg=BG, fg=FG, font=(FONT, 10)).pack(side="left")
        context_var = tk.StringVar()
        context_entry = tk.Entry(filter_row, textvariable=context_var, bg=BG_INPUT, fg=FG,
                                  insertbackground=FG, relief="flat", font=(FONT, 10), bd=0)
        context_entry.pack(side="left", fill="x", expand=True, padx=(8, 8), ipady=5)

        tree_frame = tk.Frame(dlg, bg=BG)
        tree_frame.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        records_tree = ttk.Treeview(tree_frame, columns=("time", "prompt", "version", "source", "context", "verdict"),
                                    show="headings", style="PB.Treeview")
        headings = {
            "time": "时间", "prompt": "提示词", "version": "版本",
            "source": "来源", "context": "上下文标签", "verdict": "人工裁决",
        }
        widths = {"time": 150, "prompt": 210, "version": 70, "source": 110, "context": 130, "verdict": 100}
        for column, label in headings.items():
            records_tree.heading(column, text=label)
            records_tree.column(column, width=widths[column], anchor="w")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=records_tree.yview)
        records_tree.configure(yscrollcommand=scrollbar.set)
        records_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        records_by_id = {}

        def display_verdict(value):
            return {"candidate_better": "候选更好", "baseline_better": "基线更好", "equal": "相同", "undecided": "未决定"}.get(value, "未记录")

        def refresh(*_):
            records_tree.delete(*records_tree.get_children())
            records_by_id.clear()
            filters = {"context_label": context_var.get().strip()}
            selected_verdict = verdict_values[verdict_var.get()]
            if selected_verdict:
                filters["verdict"] = selected_verdict
            for record in self._get_verification_records(**filters):
                record_id = record.get("id") or f"record_{len(records_by_id) + 1}"
                records_by_id[record_id] = record
                timestamp = record.get("captured_at") or record.get("verified_at") or "未记录"
                records_tree.insert("", "end", iid=record_id, values=(
                    timestamp[:19].replace("T", " "),
                    record.get("snippet_title", "未命名提示词"),
                    f"v{record.get('version_number', '?')}",
                    record.get("source_label") or record.get("source_type") or "未记录",
                    record.get("context_label") or "未记录",
                    display_verdict(record.get("verdict")),
                ))

        def show_selected(_event=None):
            selection = records_tree.selection()
            if not selection:
                return
            self._show_verification_record_detail(records_by_id[selection[0]])

        records_tree.bind("<Double-1>", show_selected)
        self._btn(filter_row, "筛选", refresh, ACCENT, BG, 10).pack(side="right")
        self._btn(dlg, "查看详情", show_selected, BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=18, pady=(0, 14))
        verdict_var.trace_add("write", refresh)
        context_var.trace_add("write", refresh)
        refresh()

    def _show_verification_record_detail(self, record):
        dlg = tk.Toplevel(self.win)
        dlg.title("验证记录详情")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("820x620")
        body = tk.Text(dlg, bg=BG_INPUT, fg=FG, font=(FONT, 11), wrap="word", relief="flat", bd=0)
        body.pack(fill="both", expand=True, padx=18, pady=(16, 8))
        lines = [
            f"提示词：{record.get('snippet_title', '未记录')}",
            f"版本：v{record.get('version_number', '?')}",
            f"时间：{record.get('captured_at') or record.get('verified_at') or '未记录'}",
            f"来源：{record.get('source_label') or record.get('source_type') or '未记录'}",
            f"上下文标签：{record.get('context_label') or '未记录'}",
            f"人工裁决：{record.get('verdict') or '未决定'}",
            "",
            "用户输入：",
            record.get("user_input") or "（空）",
            "",
            "上下文：",
            record.get("context_text") or "（空）",
            "",
            "基线输出：",
            record.get("baseline_output") or "（空）",
            "",
            "候选输出：",
            record.get("candidate_output") or "（空）",
            "",
            f"备注：{record.get('note') or '无'}",
        ]
        body.insert("1.0", "\n".join(lines))
        body.config(state="disabled")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _save_repair_case(self, case):
        """Persist one repair case without duplicating an existing case id."""
        self.data.setdefault("repair_cases", [])
        case_id = case.get("id")
        for index, existing in enumerate(self.data["repair_cases"]):
            if existing.get("id") == case_id:
                self.data["repair_cases"][index] = case
                self._save_data()
                return
        self.data["repair_cases"].append(case)
        self._save_data()

    def _adopt_verified_repair_case(self, case):
        """Create the formal version that corresponds to a verified repair case."""
        if case.get("adopted_version_id") is not None:
            raise ValueError("case already adopted")
        snippet = next(
            (
                item
                for item in self.snippets
                if item.get("id") == case.get("snippet_id") and not item.get("_deleted")
            ),
            None,
        )
        if snippet is None:
            raise ValueError("repair case snippet was not found")
        base_version_id = case.get("base_version_id")
        base_version = next(
            (version for version in snippet.get("versions", []) if version.get("id") == base_version_id),
            None,
        )
        if base_version is None:
            raise ValueError("repair case baseline version was not found")
        if base_version.get("version_number") != case.get("base_version_number"):
            raise ValueError("repair case baseline version does not match")
        if snippet.get("current_version_id") != base_version_id:
            raise ValueError("repair case baseline is no longer current")
        candidates = case.get("candidates", [])
        if not candidates:
            raise ValueError("repair case has no candidate")
        candidate = candidates[-1]
        new_version = self._save_new_version(
            snippet,
            snippet.get("title", "未命名提示词"),
            candidate["content"],
            snippet.get("category_id", DEFAULT_CATEGORY_ID),
            snippet.get("tag_ids", []),
            "修复案例验证通过：" + "；".join(candidate.get("change_reasons", [])),
        )
        new_version["repair_case_id"] = case["id"]
        return new_version["id"]

    def _create_repair_workbench(self, snippet=None):
        target = snippet or self.selected
        if target is None:
            raise ValueError("select a prompt before repairing it")
        if target.get("_deleted"):
            raise ValueError("cannot repair a deleted prompt")
        base_version_id = target.get("current_version_id")
        base_version = next(
            (version for version in target.get("versions", []) if version.get("id") == base_version_id),
            None,
        )
        if base_version is None:
            raise ValueError("selected prompt has no current version")
        def save_snapshot(payload):
            template = payload.get("template", "")
            variables = payload.get("variables", {})
            snapshot = create_snapshot(
                snippet_id=target.get("id", ""),
                version_id=target.get("current_version_id", ""),
                trigger=payload.get("trigger", "验证"),
                template=template,
                variable_definitions=payload.get("variable_definitions", target.get("variable_definitions", {})),
                variables=variables,
                rendered_prompt=payload.get("rendered_prompt"),
                extra=payload.get("extra"),
            )
            append_snapshot(target, snapshot)
            self._save_data()
            return snapshot["id"]

        workbench = RepairWorkbench(
            self.repair_service,
            save_case=self._save_repair_case,
            adopt_candidate=self._adopt_verified_repair_case,
            save_snapshot=save_snapshot,
        )
        workbench.ui_baseline = {
            "snippet_id": target["id"],
            "base_version_id": base_version["id"],
            "base_version_number": base_version["version_number"],
        }
        # 把基线内容与提示词自带上下文带进工作台，避免重复录入
        workbench.ui_baseline_content = base_version.get("content", "")
        workbench.ui_variable_definitions = target.get("variable_definitions", {})
        workbench.ui_context = target.get("context", "")
        return workbench

    def toggle_palette(self):
        """Toggle compact Quick Copy Palette; full workbench stays separate."""
        if self.palette_win and self.palette_win.winfo_exists():
            self._close_palette()
        else:
            self._open_palette()

    def _open_palette(self):
        """Open compact Palette: search, choose version, fill variables, copy."""
        self.data = load_prompt_data()
        self.snippets = self.data["snippets"]
        win = tk.Toplevel(self.root)
        self.palette_win = win
        win.title("快速取用")
        apply_window_icon(win)
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        width, height = 680, 650
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{width}x{height}+{(sw-width)//2}+{(sh-height)//2}")
        win.protocol("WM_DELETE_WINDOW", self._close_palette)
        win.bind("<Escape>", lambda _event: self._close_palette())

        header = tk.Frame(win, bg=BG)
        header.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(header, text="快速取用", bg=BG, fg=ACCENT,
                 font=(FONT, 16, "bold")).pack(side="left")
        tk.Label(header, text="当前版本优先 · 复制后自动收起", bg=BG, fg=FG_DIM,
                 font=(FONT, 10)).pack(side="left", padx=(12, 0))
        self._btn(header, "最近调用", lambda: self._palette_set_sort("recent"),
                  BG, FG_DIM, 9).pack(side="right", padx=2)
        self._btn(header, "收藏优先", lambda: self._palette_set_sort("favorite"),
                  BG, FG_DIM, 9).pack(side="right", padx=2)
        self._btn(header, "设置", self._show_hotkey_settings,
                  BG, FG_DIM, 9).pack(side="right", padx=2)

        search_label = tk.Label(win, text="搜索提示词（标题、标签、分类、正文）", bg=BG, fg=FG_DIM,
                                font=(FONT, 9), anchor="w")
        search_label.pack(fill="x", padx=16)
        search_var = tk.StringVar()
        search_entry = tk.Entry(
            win, textvariable=search_var, bg=BG_INPUT, fg=FG, insertbackground=FG,
            relief="flat", font=(FONT, 13), bd=0, highlightthickness=0,
            selectbackground=ACCENT, selectforeground=BG,
        )
        search_entry.pack(fill="x", padx=16, ipady=8, pady=(2, 8))
        self.palette_search_var = search_var

        list_frame = tk.Frame(win, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=16)
        canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        cards = tk.Frame(canvas, bg=BG)
        cards.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=cards, anchor="nw", width=628)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.palette_cards = cards
        self.palette_canvas = canvas

        self.palette_detail = tk.Frame(win, bg=BG_PANEL)
        self.palette_detail.pack(fill="x", padx=16, pady=(8, 0))
        self.palette_feedback = tk.Label(win, text="", bg=BG, fg=ACCENT_2, font=(FONT, 9))
        self.palette_feedback.pack(fill="x", padx=16, pady=(3, 0))
        footer = tk.Frame(win, bg=BG)
        footer.pack(fill="x", padx=16, pady=(5, 12))
        tk.Label(footer, text="Enter 复制 · Esc 收起 · 不自动粘贴", bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).pack(side="left")
        self._btn(footer, "打开完整工作台", self._open_full_from_palette,
                  BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="right")

        search_var.trace_add("write", lambda *_args: self._render_palette_results(search_var.get()))
        search_entry.bind("<Return>", lambda _event: self._palette_copy_selected())
        search_entry.focus_set()
        self._render_palette_results("")

    def _close_palette(self):
        if self.palette_win and self.palette_win.winfo_exists():
            self.palette_win.destroy()
        self.palette_win = None
        self.palette_selected = None
        self.palette_selected_version_id = None
        self.palette_variable_entries = {}

    def _palette_set_sort(self, mode):
        self.palette_sort_mode = mode
        self._render_palette_results(self.palette_search_var.get())

    def _render_palette_results(self, query=""):
        if not self.palette_win or not self.palette_win.winfo_exists():
            return
        for child in self.palette_cards.winfo_children():
            child.destroy()
        self.palette_results = self._quick_copy_search(query, self.palette_sort_mode)
        if not self.palette_results:
            tk.Label(self.palette_cards, text="没有匹配的提示词", bg=BG, fg=FG_DIM,
                     font=(FONT, 11)).pack(anchor="w", pady=18)
        else:
            for snippet in self.palette_results:
                self._render_palette_card(snippet)
        self._render_palette_detail()

    def _render_palette_card(self, snippet):
        selected = self.palette_selected and self.palette_selected.get("id") == snippet.get("id")
        card_bg = BG_PANEL if selected else BG_INPUT
        card = tk.Frame(self.palette_cards, bg=card_bg, bd=1, relief="solid",
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 7))
        header = tk.Frame(card, bg=card_bg)
        header.pack(fill="x", padx=10, pady=(7, 2))
        title = str(snippet.get("title", "未命名提示词"))
        title_text = ("★ " if snippet.get("is_favorite") else "") + title[:34]
        tk.Label(header, text=title_text, bg=card_bg, fg=ACCENT,
                 font=(FONT, 11, "bold"), anchor="w").pack(side="left")
        current = get_selected_version(snippet)
        version_text = f"v{current.get('version_number', '?')}" if current else "无版本"
        if snippet.get("stable_version_id") == snippet.get("current_version_id"):
            version_text += " · 稳定"
        tk.Label(header, text=version_text, bg=card_bg, fg=FG_DIM,
                 font=(FONT, 9)).pack(side="right")
        category = snippet.get("_quick_copy_category_name", "")
        tags = " ".join(f"#{name}" for name in snippet.get("_quick_copy_tag_names", []))
        meta = " · ".join(value for value in [category, tags] if value)
        tk.Label(card, text=meta or "未分类", bg=card_bg, fg=FG_DIM,
                 font=(FONT, 9), anchor="w").pack(fill="x", padx=10)
        preview = version_content(snippet).replace("\n", " ")
        tk.Label(card, text=preview[:100] + ("..." if len(preview) > 100 else ""),
                 bg=card_bg, fg=FG, font=(FONT, 10), anchor="w", justify="left").pack(fill="x", padx=10, pady=(2, 7))
        for widget in (card, header):
            widget.bind("<Button-1>", lambda _event, item=snippet: self._select_palette_item(item))

    def _select_palette_item(self, snippet):
        self.palette_selected = snippet
        self.palette_selected_version_id = snippet.get("current_version_id")
        self._render_palette_results(self.palette_search_var.get())

    def _render_palette_detail(self):
        if not getattr(self, "palette_detail", None) or not self.palette_detail.winfo_exists():
            return
        for child in self.palette_detail.winfo_children():
            child.destroy()
        snippet = self.palette_selected
        if not snippet:
            tk.Label(self.palette_detail, text="选择一条提示词查看版本和变量", bg=BG_PANEL, fg=FG_DIM,
                     font=(FONT, 9)).pack(anchor="w", padx=10, pady=7)
            return
        version_id = self.palette_selected_version_id or snippet.get("current_version_id")
        version = get_selected_version(snippet, version_id)
        current = get_selected_version(snippet)
        header = tk.Frame(self.palette_detail, bg=BG_PANEL)
        header.pack(fill="x", padx=10, pady=(7, 2))
        tk.Label(header, text=f"已选：{snippet.get('title', '未命名提示词')}", bg=BG_PANEL, fg=ACCENT,
                 font=(FONT, 10, "bold")).pack(side="left")
        favorite_text = "取消收藏" if snippet.get("is_favorite") else "收藏"
        self._btn(header, favorite_text, lambda: self._palette_toggle_favorite(snippet),
                  BTN_SECONDARY, BTN_SECONDARY_FG, 9).pack(side="right")
        if current:
            self._btn(header, f"当前 v{current.get('version_number', '?')}",
                      lambda: self._palette_choose_version(snippet, current.get("id")),
                      ACCENT_2, BG, 9).pack(side="right", padx=2)
        tk.Label(self.palette_detail, text="历史版本", bg=BG_PANEL, fg=FG_DIM,
                 font=(FONT, 9), anchor="w").pack(fill="x", padx=10)
        history = sorted(snippet.get("versions", []), key=lambda item: item.get("version_number", 0), reverse=True)
        history_row = tk.Frame(self.palette_detail, bg=BG_PANEL)
        history_row.pack(fill="x", padx=10, pady=(2, 4))
        for item in history[:6]:
            label = f"v{item.get('version_number', '?')}" + (" · 当前" if item.get("id") == snippet.get("current_version_id") else "")
            self._btn(history_row, label, lambda item_id=item.get("id"): self._palette_choose_version(snippet, item_id),
                      ACCENT if item.get("id") == version_id else BTN_SECONDARY,
                      BG if item.get("id") == version_id else BTN_SECONDARY_FG, 9).pack(side="left", padx=(0, 3))
        content = version.get("content", "") if version else ""
        template = PromptTemplate.from_text(content, snippet.get("variable_definitions", {}))
        self.palette_variable_entries = {}
        if template.variables:
            tk.Label(self.palette_detail, text="填写变量（可选）", bg=BG_PANEL, fg=WARN,
                     font=(FONT, 9, "bold"), anchor="w").pack(fill="x", padx=10, pady=(1, 2))
            for variable in template.variables:
                row = tk.Frame(self.palette_detail, bg=BG_PANEL)
                row.pack(fill="x", padx=10, pady=1)
                tk.Label(row, text=variable["name"], bg=BG_PANEL, fg=FG_DIM,
                         font=(FONT, 9), width=16, anchor="w").pack(side="left")
                entry = tk.Entry(row, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                                 font=(FONT, 9), bd=0, highlightthickness=0)
                entry.pack(side="left", fill="x", expand=True, ipady=3)
                if variable.get("example"):
                    entry.insert(0, variable["example"])
                self.palette_variable_entries[variable["name"]] = entry
        buttons = tk.Frame(self.palette_detail, bg=BG_PANEL)
        buttons.pack(fill="x", padx=10, pady=(4, 8))
        self._btn(buttons, "复制原文", lambda: self._palette_copy_text(snippet, version_id, content),
                  BTN_SECONDARY, BTN_SECONDARY_FG, 9).pack(side="right", padx=2)
        if template.variables:
            self._btn(buttons, "填充并复制", lambda: self._palette_fill_copy(snippet, version_id, template),
                      ACCENT, BG, 9, True).pack(side="right", padx=2)

    def _palette_choose_version(self, snippet, version_id):
        self.palette_selected_version_id = version_id
        self._render_palette_results(self.palette_search_var.get())

    def _palette_authoritative_snippet(self, snippet):
        return next((item for item in self.snippets if item.get("id") == snippet.get("id")), snippet)

    def _palette_toggle_favorite(self, snippet):
        target = self._palette_authoritative_snippet(snippet)
        target["is_favorite"] = not bool(target.get("is_favorite"))
        self._save_data()
        self.palette_selected = target
        self._render_palette_results(self.palette_search_var.get())

    def _palette_make_snapshot(self, snippet, version_id, template, rendered):
        target = self._palette_authoritative_snippet(snippet)
        values = {name: entry.get() for name, entry in self.palette_variable_entries.items()}
        snapshot = create_snapshot(target.get("id", ""), version_id or "", "Quick Copy 填充并复制",
                                   template.text, template.definitions(), values, rendered_prompt=rendered)
        append_snapshot(target, snapshot)
        self._save_data()
        return snapshot

    def _palette_copy_text(self, snippet, version_id, content, variable_snapshot_id=None):
        if not content or not copy_to_clipboard(content):
            self.palette_feedback.config(text="复制失败，未记录调用")
            return False
        self._record_quick_copy(snippet.get("id", ""), version_id, variable_snapshot_id)
        self.palette_feedback.config(text="复制成功 · 已记录本次调用")
        self.palette_win.after(450, self._close_palette)
        return True

    def _palette_fill_copy(self, snippet, version_id, template):
        try:
            values = {name: entry.get() for name, entry in self.palette_variable_entries.items()}
            rendered = template.render(values)
        except ValueError as exc:
            self.palette_feedback.config(text=str(exc))
            return False
        try:
            snapshot = self._palette_make_snapshot(snippet, version_id, template, rendered)
        except (ValueError, OSError) as exc:
            self.palette_feedback.config(text=f"变量快照保存失败：{exc}")
            return False
        return self._palette_copy_text(snippet, version_id, rendered, snapshot.get("id"))

    def _palette_copy_selected(self):
        snippet = self.palette_selected
        if not snippet and getattr(self, "palette_results", None):
            snippet = self.palette_results[0]
        if not snippet:
            return False
        version_id = self.palette_selected_version_id or snippet.get("current_version_id")
        return self._palette_copy_text(snippet, version_id, get_prompt_version_content(snippet, version_id))

    def _open_full_from_palette(self):
        self._close_palette()
        self._open()

    def _show_hotkey_settings(self):
        """Edit Palette global hotkey; failed registration restores old value."""
        current = self.data.get("preferences", {}).get("quick_copy_hotkey", HOTKEY)
        value = simpledialog.askstring("快捷键设置", "输入全局快捷键（如 ctrl+shift+space）：",
                                       initialvalue=current, parent=self.palette_win or self.win)
        if value is None:
            return False
        value = value.strip().lower()
        try:
            Win32HotkeyManager._key_parts(value)
        except ValueError as exc:
            messagebox.showwarning("快捷键不可用", str(exc), parent=self.palette_win or self.win)
            return False
        self.data.setdefault("preferences", {})["quick_copy_hotkey"] = value
        self._save_data()
        if getattr(self, "palette_feedback", None) and self.palette_feedback.winfo_exists():
            self.palette_feedback.config(text="快捷键已保存；重启后生效")
        return True

    def _open_repair_workbench(self):
        if self.win is None:
            raise RuntimeError("PromptBox window is unavailable")
        try:
            self._create_repair_workbench().open_toplevel(self.win, theme=T)
        except ValueError as exc:
            messagebox.showinfo("调优提示词", f"{exc}。\n\n请先在列表中选择一个提示词，再点「调优提示词」。", parent=self.win)

    def _reload_data(self):
        self.data = load_prompt_data()
        self.snippets = self.data["snippets"]

    def _save_data(self):
        self.data["snippets"] = self.snippets
        self.data.setdefault("runs", [])
        self.data.setdefault("repair_cases", [])
        self.data.setdefault("preferences", {})
        save_prompt_data(self.data)
        self._reload_data()

    def _update_sort_label(self):
        key, label, desc = SORT_OPTIONS[self.sort_idx]
        arrow = " ↓" if desc else " ↑"
        if hasattr(self, "sort_label"):
            self.sort_label.config(text=f"排序: {label}{arrow}")

    def _cycle_sort(self):
        self.sort_idx = (self.sort_idx + 1) % len(SORT_OPTIONS)
        self._update_sort_label()
        self._filter()

    def _cycle_theme(self):
        global CURRENT_THEME, T, BG, BG_INPUT, BG_PANEL, BG_HOVER, FG, FG_DIM, ACCENT, ACCENT_2, WARN, DANGER, BORDER, BTN_SECONDARY, BTN_SECONDARY_FG, BTN_DANGER, BTN_DANGER_FG
        keys = list(THEMES.keys())
        idx = keys.index(CURRENT_THEME)
        CURRENT_THEME = keys[(idx + 1) % len(keys)]
        T = THEMES[CURRENT_THEME]

        BG = T["bg"]
        BG_INPUT = T["bg_input"]
        BG_PANEL = T["bg_panel"]
        BG_HOVER = T["bg_hover"]
        FG = T["fg"]
        FG_DIM = T["fg_dim"]
        ACCENT = T["accent"]
        ACCENT_2 = T["accent_2"]
        WARN = T["warn"]
        DANGER = T["danger"]
        BORDER = T["border"]
        BTN_SECONDARY = T.get("btn_secondary", BG_INPUT)
        BTN_SECONDARY_FG = T.get("btn_secondary_fg", FG)
        BTN_DANGER = T.get("btn_danger", DANGER)
        BTN_DANGER_FG = T.get("btn_danger_fg", BG)
        
        if self.win and self.win.winfo_exists():
            self.win.configure(bg=BG)
            self.theme_label.config(text=f"🎨 {T['name'].split()[0]}", fg=ACCENT_2)
            self._apply_theme_to_widgets()

    def _apply_theme_to_widgets(self):
        # 递归更新所有控件颜色
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("PB.Treeview", background=BG_INPUT, foreground=FG,
                        fieldbackground=BG_INPUT, font=(FONT, 12), rowheight=34, borderwidth=0)
        style.configure("PB.Treeview.Heading", background=BG_PANEL, foreground=FG,
                        font=(FONT, 12, "bold"), borderwidth=0)
        style.map("PB.Treeview", background=[("selected", ACCENT)], foreground=[("selected", BG)])

        # 主窗口元素
        for w in self.win.winfo_children():
            self._style_widget(w)

        # 强制刷新
        self._filter()
        self._update_tags()

    def _style_widget(self, w):
        wclass = w.winfo_class()
        
        # 依据控件类进行着色
        if wclass == "Frame":
            # 排除特定的子框架或者设定为主背景
            w.configure(bg=BG)
            for child in w.winfo_children():
                self._style_widget(child)
        elif wclass == "Label":
            # 依据文字特征和之前的配置进行变色
            cur_fg = w.cget("foreground")
            # 根据特征对应新配色
            if w == self.count_label or w.cget("text").startswith("↑") or w.cget("text").startswith("排序"):
                w.configure(bg=BG, fg=FG_DIM)
            elif w.cget("text") == "PromptBox":
                w.configure(bg=BG, fg=ACCENT)
            elif w.cget("text") == "分类":
                w.configure(bg=BG_PANEL, fg=FG)
            elif w.cget("text").startswith("已选"):
                w.configure(bg=BG, fg=ACCENT)
            elif w.cget("text").startswith("标签筛选"):
                w.configure(bg=BG, fg=FG_DIM)
            elif w.cget("text").startswith("#"):
                w.configure(bg=BG_INPUT, fg=FG_DIM)
            else:
                w.configure(bg=BG, fg=FG)
        elif wclass == "Entry":
            w.configure(bg=BG_INPUT, fg=FG, insertbackground=FG,
                        highlightthickness=0, selectbackground=ACCENT, selectforeground=BG)
        elif wclass == "Text":
            w.configure(bg=BG_INPUT, fg=FG, insertbackground=FG,
                        highlightthickness=0, selectbackground=ACCENT, selectforeground=BG)
        elif wclass == "Button":
            txt = w.cget("text")
            if txt in ["＋ 新建", "保存", "复制", "填充并复制", "＋ 新标签"]:
                w.configure(bg=ACCENT, fg=BG, activebackground=ACCENT, activeforeground=BG)
            elif txt == "+ 智能标签":
                w.configure(bg=ACCENT_2, fg=BG, activebackground=ACCENT_2, activeforeground=BG)
            elif txt in ["退出", "删除"]:
                w.configure(bg=BTN_DANGER, fg=BTN_DANGER_FG, activebackground=BTN_DANGER, activeforeground=BTN_DANGER_FG)
            else:
                w.configure(bg=BTN_SECONDARY, fg=BTN_SECONDARY_FG, activebackground=BTN_SECONDARY, activeforeground=BTN_SECONDARY_FG)
        elif wclass == "Treeview":
            # style 会自动应用
            pass
        else:
            # 其它嵌套容器
            try:
                w.configure(bg=BG)
            except Exception:
                pass
            for child in w.winfo_children():
                self._style_widget(child)

    def _sorted(self, items):
        key, _, desc = SORT_OPTIONS[self.sort_idx]
        if key == "title":
            return sorted(items, key=lambda s: s.get("title", "").lower(), reverse=desc)
        return sorted(items, key=lambda s: s.get(key, ""), reverse=desc)

    def _category_prompt_count(self, category_id):
        ids = collect_category_descendants(self.data, category_id)
        return sum(1 for s in self.snippets if s.get("category_id") in ids and not s.get("_deleted"))

    def _restore_category_selection(self):
        """保留用户的分类状态；无效分类自动回到全部提示词。"""
        if self.active_category_id is None:
            return
        category_ids = {c.get("id") for c in self.data.get("categories", [])}
        if self.active_category_id not in category_ids:
            self.active_category_id = None

    def _render_categories(self):
        self.cat_tree.delete(*self.cat_tree.get_children())
        by_parent = {}
        for c in self.data.get("categories", []):
            by_parent.setdefault(c.get("parent_id"), []).append(c)

        def add_nodes(parent_iid, parent_id):
            for c in sorted(by_parent.get(parent_id, []), key=lambda x: x.get("name", "")):
                count = self._category_prompt_count(c["id"])
                text = f"📁 {c['name']}  {count}"
                self.cat_tree.insert(parent_iid, "end", iid=c["id"], text=text, open=True)
                add_nodes(c["id"], c["id"])

        add_nodes("", None)
        if self.active_category_id and self.cat_tree.exists(self.active_category_id):
            self.cat_tree.selection_set(self.active_category_id)
            self.cat_tree.focus(self.active_category_id)

    def _category_clear_label(self):
        return "查看全部"

    def _on_category_select(self):
        sel = self.cat_tree.selection()
        if not sel:
            self._deselect_category()
            return
        self.active_category_id = sel[0]
        self._clear_frames()
        self._filter()

    def _category_menu(self, event):
        iid = self.cat_tree.identify_row(event.y)
        if iid:
            self.cat_tree.selection_set(iid)
            self.active_category_id = iid
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG, activebackground=ACCENT, activeforeground=BG)
        menu.add_command(label="新建子分类", command=lambda: self._add_category(top_level=False))
        menu.add_command(label="新建一级分类", command=lambda: self._add_category(top_level=True))
        menu.add_separator()
        if self.active_category_id:
            menu.add_command(label="取消选中（查看全部）", command=self._deselect_category)
            menu.add_command(label="重命名分类", command=self._rename_category)
            menu.add_command(label="删除分类", command=self._delete_category)
        else:
            menu.add_command(label="取消选中（查看全部）", command=self._deselect_category)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _ask_string(self, title, prompt, initialvalue="", parent=None):
        """自定义主题输入弹窗：替代系统原生丑陋 simpledialog.askstring"""
        p = parent or self.win
        dlg = tk.Toplevel(p)
        dlg.title(title)
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        # 窗口大小与居中（紧贴 prompt 的实际文本长度动态适配）
        tk.Label(dlg, text=prompt, bg=BG, fg=FG, font=(FONT, 12)).pack(anchor="w", padx=18, pady=(16, 6))
        e = tk.Entry(dlg, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 13), bd=0, highlightthickness=0,
                     selectbackground=ACCENT, selectforeground=BG)
        e.pack(fill="x", padx=18, ipady=6)
        e.insert(0, initialvalue)
        if initialvalue:
            e.select_range(0, "end")
        e.focus_set()

        # 按钮区
        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=18, pady=(12, 16))

        result = [None]  # 闭包引用

        def ok():
            result[0] = e.get().strip()
            dlg.destroy()

        def cancel():
            dlg.destroy()

        self._btn(bf, "确定", ok, ACCENT, BG, 12, True).pack(side="right", padx=(6, 0))
        self._btn(bf, "取消", cancel, BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right")

        e.bind("<Return>", lambda ev: ok())
        e.bind("<Escape>", lambda ev: cancel())

        # 计算窗口宽度（至少 320px）并居中
        w = max(320, len(prompt) * 14 + 72)
        h = 180
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        # 模态等待
        dlg.transient(p)
        dlg.grab_set()
        p.wait_window(dlg)
        return result[0]

    def _add_category(self, parent_id=None, top_level=False):
        if parent_id is None:
            parent_id = self.active_category_id
        if top_level:
            parent_id = None
        name = self._ask_string("新建分类", "分类名称：")
        if not name:
            return
        cid = make_id("cat", name)
        existing = {c["id"] for c in self.data["categories"]}
        n = 2
        original = cid
        while cid in existing:
            cid = f"{original}_{n}"
            n += 1
        self.data["categories"].append({"id": cid, "name": name.strip(), "parent_id": parent_id, "children": []})
        self.data["categories"] = ensure_category_links(self.data["categories"])
        save_prompt_data(self.data)
        self._reload_data()
        self.active_category_id = cid
        self._render_categories()
        self._filter()

    def _popup_new_category_menu(self):
        if self.active_category_id:
            # 有选中：弹 2 项精简菜单
            menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG, activebackground=ACCENT, activeforeground=BG)
            menu.add_command(label="新建子分类", command=lambda: self._add_category(top_level=False))
            menu.add_command(label="新建一级分类", command=lambda: self._add_category(top_level=True))
            try:
                x = self._new_cat_btn.winfo_rootx()
                y = self._new_cat_btn.winfo_rooty() + self._new_cat_btn.winfo_height()
                menu.tk_popup(x, y)
            finally:
                menu.grab_release()
        else:
            # 无选中：直接弹输入框，等价于新建一级分类
            self._add_category(top_level=True)

    def _on_category_click(self, event):
        """点击分类树时支持取消选中（点击空白处或根行）。"""
        row = self.cat_tree.identify_row(event.y)
        if not row:
            # 点击空白处：取消选中
            self.cat_tree.selection_set(())
            self._deselect_category()

    def _deselect_category(self):
        """主动取消选中的分类，回到「全部提示词」视图。"""
        self.active_category_id = None
        self._clear_frames()
        self._filter()

    def _rename_category(self):
        cid = self.active_category_id
        cat = next((c for c in self.data["categories"] if c["id"] == cid), None)
        if not cat:
            return
        name = self._ask_string("重命名分类", "分类名称：", initialvalue=cat["name"])
        if not name:
            return
        cat["name"] = name.strip()
        save_prompt_data(self.data)
        self._reload_data()
        self._render_categories()

    def _delete_category(self):
        cid = self.active_category_id
        if cid == DEFAULT_CATEGORY_ID:
            messagebox.showwarning("不能删除", "未分类是兜底分类，不能删除。", parent=self.win)
            return
        ids = collect_category_descendants(self.data, cid)
        count = sum(1 for s in self.snippets if s.get("category_id") in ids and not s.get("_deleted"))
        if not messagebox.askyesno("删除分类", f"删除该分类及子分类？\n其中 {count} 条提示词会移动到未分类。", parent=self.win):
            return
        self.data["categories"] = [c for c in self.data["categories"] if c["id"] not in ids]
        for s in self.snippets:
            if s.get("category_id") in ids:
                s["category_id"] = DEFAULT_CATEGORY_ID
                s["updated_at"] = now()
        self.data["categories"] = ensure_category_links(self.data["categories"])
        self.data["snippets"] = self.snippets
        save_prompt_data(self.data)
        self._reload_data()
        self.active_category_id = DEFAULT_CATEGORY_ID
        self._render_categories()
        self._filter()

    def _update_tags(self):
        for w in self.tags_frame.winfo_children():
            w.destroy()
        tk.Label(self.tags_frame, text="标签筛选：", bg=BG, fg=FG_DIM, font=(FONT, 10)).pack(side="left", padx=(0, 6))
        all_tags = sorted(self.data.get("tags", []), key=lambda t: t["name"])
        btn = tk.Label(self.tags_frame, text="全部", bg=BG_INPUT if not self.active_tags else BG,
                       fg=FG if not self.active_tags else FG_DIM,
                       font=(FONT, 10), padx=8, pady=2, cursor="hand2")
        btn.pack(side="left", padx=(0, 4))
        btn.bind("<Button-1>", lambda e: self._clear_tag_filter())
        for tag in all_tags:
            used = any(tag["id"] in s.get("tag_ids", []) for s in self.snippets)
            active = tag["id"] in self.active_tags
            bg = ACCENT if active else BG_INPUT
            fg = BG if active else (FG_DIM if used else "#555555")
            label = tk.Label(self.tags_frame, text=tag["name"], bg=bg, fg=fg,
                             font=(FONT, 10), padx=8, pady=2, cursor="hand2")
            label.pack(side="left", padx=(0, 4))
            label.bind("<Button-1>", lambda e, tid=tag["id"]: self._toggle_tag_filter(tid))

    def _clear_tag_filter(self):
        self.active_tags.clear()
        self._update_tags()
        self._filter()

    def _toggle_tag_filter(self, tag_id):
        # 单选焦点模式：点已激活标签取消筛选；否则切到该标签
        if tag_id in self.active_tags and len(self.active_tags) == 1:
            self.active_tags.clear()
        else:
            self.active_tags.clear()
            self.active_tags.add(tag_id)
        self._update_tags()
        self._filter()

    def _has_subcategories(self, category_id):
        for c in self.data.get("categories", []):
            if c.get("parent_id") == category_id:
                return True
        return False

    def _show_list_view(self):
        if self._mode != "list":
            if self.tag_view_frame:
                self.tag_view_frame.pack_forget()
        self.list_container.pack(fill="both", expand=True)
        self._mode = "list"

    def _hide_list_view(self):
        self.list_container.pack_forget()

    def _clear_tag_view(self):
        if not self.tag_view_frame:
            return
        for w in self.tag_view_frame.winfo_children():
            w.destroy()

    def _clear_substructure(self):
        # deprecated：保留空实现，防外部调用崩溃
        pass

    def _render_subcat_bar(self, category_id):
        """列表视图顶部的子分类跳转条（chip 形式，含当前选中态高亮）"""
        # 清空
        for w in self.subcat_bar.winfo_children():
            w.destroy()

        if not category_id:
            self.subcat_bar.pack_forget()
            return

        # 找该分类的直接子分类
        subcats = [c for c in self.data.get("categories", [])
                   if c.get("parent_id") == category_id]
        # 找该分类的兄弟分类（如果当前分类本身是子分类，展示同级）
        current_cat = next((c for c in self.data.get("categories", []) if c["id"] == category_id), None)
        siblings = []
        if current_cat and current_cat.get("parent_id"):
            siblings = [c for c in self.data.get("categories", [])
                        if c.get("parent_id") == current_cat["parent_id"]
                        and c["id"] != category_id]

        # 无子分类也无兄弟时，藏起来
        if not subcats and not siblings:
            self.subcat_bar.pack_forget()
            return

        self.subcat_bar.pack(fill="x", padx=8, pady=(4, 4), before=self.list_container)
        # 面包屑：回到父分类
        if current_cat and current_cat.get("parent_id"):
            parent_id = current_cat["parent_id"]
            parent_name = category_name(self.data, parent_id)
            back = tk.Label(self.subcat_bar, text=f"◄ {parent_name}",
                            bg=BG, fg=FG_DIM, font=(FONT, 10),
                            cursor="hand2", padx=8, pady=3)
            back.pack(side="left", padx=(0, 8))
            back.bind("<Button-1>",
                      lambda e, pid=parent_id: self._jump_to_category(pid))

        # 展示的分类列表：优先展示子分类，无则展示兄弟
        chips = subcats if subcats else siblings
        chip_label = "子分类：" if subcats else "同级："
        tk.Label(self.subcat_bar, text=chip_label, bg=BG, fg=FG_DIM,
                 font=(FONT, 10)).pack(side="left", padx=(0, 4))

        for sc in sorted(chips, key=lambda x: x.get("name", "")):
            ids = collect_category_descendants(self.data, sc["id"])
            count = sum(1 for s in self.snippets
                        if s.get("category_id") in ids and not s.get("_deleted"))
            is_active = (sc["id"] == self.active_category_id)
            chip_bg = ACCENT if is_active else BG_INPUT
            chip_fg = BG if is_active else FG
            chip = tk.Label(self.subcat_bar,
                            text=f"📁 {sc['name']} · {count}",
                            bg=chip_bg, fg=chip_fg,
                            font=(FONT, 10), padx=10, pady=3,
                            cursor="hand2")
            chip.pack(side="left", padx=(0, 4))
            chip.bind("<Button-1>",
                      lambda e, cid=sc["id"]: self._jump_to_category(cid))

    def _jump_to_category(self, category_id):
        """chip 跳转到指定分类"""
        self.active_category_id = category_id
        try:
            self.cat_tree.selection_set(category_id)
            self.cat_tree.focus(category_id)
            self.cat_tree.see(category_id)
        except Exception:
            pass
        self._clear_frames()
        self._filter()

    def _render_substructure(self, category_id):
        self._hide_list_view()
        if self.tag_view_frame:
            self.tag_view_frame.pack_forget()
        self._mode = "substructure"
        self._clear_substructure()
        self.substructure_frame.pack(fill="both", expand=True, padx=4, pady=4)
        cat_name = category_name(self.data, category_id)
        header = tk.Frame(self.substructure_frame, bg=BG)
        header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(header, text=f"\u300c{cat_name}\u300d下的内容", bg=BG, fg=FG_DIM,
                 font=(FONT, 10)).pack(side="left")
        wrap = tk.Frame(self.substructure_frame, bg=BG)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        inner = tk.Frame(canvas, bg=BG)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        def _on_resize(e):
            canvas.itemconfig(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_resize)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        def _on_wheel(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        self._sub_canvas = canvas
        # 1) 子分类卡片
        subcats = [c for c in self.data.get("categories", [])
                   if c.get("parent_id") == category_id]
        if subcats:
            tk.Label(inner, text="子分类", bg=BG, fg=FG,
                     font=(FONT, 11, "bold")).pack(anchor="w", padx=8, pady=(8, 4))
            for sc in sorted(subcats, key=lambda x: x.get("name", "")):
                self._render_subcat_card(inner, sc)
        # 2) 直接归属该分类的提示词
        direct = [s for s in self.snippets
                  if not s.get("_deleted")
                  and s.get("category_id") == category_id]
        if direct:
            tk.Label(inner, text="当前分类内的提示词", bg=BG, fg=FG,
                     font=(FONT, 11, "bold")).pack(anchor="w", padx=8, pady=(16, 4))
            for s in self._sorted(direct):
                self._render_direct_snippet_card(inner, s)
        if not subcats and not direct:
            tk.Label(inner, text="\uff08空\uff09", bg=BG, fg=FG_DIM,
                     font=(FONT, 11)).pack(anchor="w", padx=8, pady=24)

    def _render_subcat_card(self, parent, cat):
        ids = collect_category_descendants(self.data, cat["id"])
        count = sum(1 for s in self.snippets
                    if s.get("category_id") in ids and not s.get("_deleted"))
        card = tk.Frame(parent, bg=BG_INPUT, cursor="hand2",
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=8, pady=4)
        row = tk.Frame(card, bg=BG_INPUT)
        row.pack(fill="x", padx=14, pady=10)
        tk.Label(row, text="\U0001F4C1  " + cat["name"], bg=BG_INPUT, fg=FG,
                 font=(FONT, 13, "bold")).pack(side="left")
        tk.Label(row, text=f"{count} 条", bg=BG_INPUT, fg=FG_DIM,
                 font=(FONT, 11)).pack(side="right")
        def on_click(e, cid=cat["id"]):
            self.active_category_id = cid
            self.cat_tree.selection_set(cid)
            self.cat_tree.focus(cid)
            self._clear_frames()
            self._filter()
        card.bind("<Button-1>", on_click)
        for child in row.winfo_children():
            child.bind("<Button-1>", on_click)
        def on_enter(e, c=card): c.configure(bg=BG_HOVER, highlightbackground=ACCENT)
        def on_leave(e, c=card): c.configure(bg=BG_INPUT, highlightbackground=BORDER)
        card.bind("<Enter>", on_enter)
        card.bind("<Leave>", on_leave)

    def _render_direct_snippet_card(self, parent, s):
        content = prompt_display_content(s)
        card = tk.Frame(parent, bg=BG_INPUT, cursor="hand2",
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=8, pady=4)
        row = tk.Frame(card, bg=BG_INPUT)
        row.pack(fill="x", padx=14, pady=10)
        tk.Label(row, text=s.get("title", "未命名"), bg=BG_INPUT, fg=FG,
                 font=(FONT, 12, "bold"), anchor="w").pack(side="left", fill="x", expand=True)
        ver = snippet_version_label(s)
        tk.Label(row, text=ver, bg=ACCENT_2, fg=BG,
                 font=(FONT, 9, "bold"), padx=6, pady=1).pack(side="right", padx=(8, 0))
        preview = (content or "")[:120].replace("\n", " ")
        if len(content or "") > 120:
            preview += "..."
        body = tk.Frame(card, bg=BG_INPUT)
        body.pack(fill="x", padx=14, pady=(0, 10))
        tk.Label(body, text=preview, bg=BG_INPUT, fg=FG_DIM,
                 font=(FONT, 10), anchor="w", wraplength=680, justify="left").pack(fill="x")

        def on_click(e, sn=s):
            self._edit_snippet(sn)

        def on_right(e, sn=s):
            self._show_card_menu(e, sn)

        # 单击卡片 → 编辑；右键 → 上下文菜单（编辑/删除/历史/重命名/复制）
        for widget in (card, row, body, *row.winfo_children(), *body.winfo_children()):
            widget.bind("<Button-1>", on_click)
            widget.bind("<Button-3>", on_right)

        def on_enter(e, c=card): c.configure(bg=BG_HOVER, highlightbackground=ACCENT)
        def on_leave(e, c=card): c.configure(bg=BG_INPUT, highlightbackground=BORDER)
        card.bind("<Enter>", on_enter)
        card.bind("<Leave>", on_leave)

    def _show_card_menu(self, event, s):
        """卡片视图（子分类/标签视图）通用右键菜单"""
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG)
        menu.add_command(label="编辑", command=lambda: self._edit_snippet(s))
        content = prompt_display_content(s) or ""
        if content:
            menu.add_command(label="复制内容", command=lambda: self._do_copy(content))
        menu.add_command(label="历史版本", command=lambda: self._show_version_history(s))
        menu.add_command(label="重命名", command=lambda: self._rename_snippet(s))
        menu.add_separator()
        menu.add_command(label="删除", command=lambda: self._del_snippet(s))
        menu.tk_popup(event.x_root, event.y_root)

    def _fmt_time(self, iso_str):
        """把 ISO 时间格式化成 MM/DD HH:MM"""
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%m/%d %H:%M")
        except Exception:
            return ""

    def _render_tag_view(self):
        """得到大脑式卡片视图：单标签焦点下的所有提示词"""
        self._hide_list_view()
        if self.substructure_frame:
            self.substructure_frame.pack_forget()
        self._mode = "tagview"
        self._clear_tag_view()
        self.tag_view_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # 顶部：返回箭头 + 标题
        tag_names_map = tag_id_to_name(self.data)
        active_tag_id = next(iter(self.active_tags))
        active_tag_name = tag_names_map.get(active_tag_id, "标签")

        # 过滤当前标签下的提示词
        cards = [s for s in self._sorted(self.snippets)
                 if not s.get("_deleted")
                 and active_tag_id in s.get("tag_ids", [])]

        header = tk.Frame(self.tag_view_frame, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 6))
        back_btn = tk.Label(header, text="◄", bg=BG, fg=FG,
                            font=(FONT, 14), cursor="hand2", padx=6)
        back_btn.pack(side="left")
        back_btn.bind("<Button-1>", lambda e: self._clear_tag_filter())
        tk.Label(header, text=f"「{active_tag_name}」标签下的 {len(cards)} 条提示词",
                 bg=BG, fg=FG, font=(FONT, 14, "bold")).pack(side="left", padx=(6, 0))

        # 滚动区
        wrap = tk.Frame(self.tag_view_frame, bg=BG)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        inner = tk.Frame(canvas, bg=BG)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        def _on_resize(e):
            canvas.itemconfig(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_resize)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        def _on_wheel(e):
            canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)

        if not cards:
            tk.Label(inner, text="（该标签下暂无提示词）", bg=BG, fg=FG_DIM,
                     font=(FONT, 11)).pack(anchor="w", padx=12, pady=24)
            return

        for s in cards:
            self._render_tag_view_card(inner, s, tag_names_map)

    def _render_tag_view_card(self, parent, s, tag_names_map):
        """得到大脑式提示词卡片：标题（大） + 预览（3行） + 底部标签行 + 右下时间"""
        content = prompt_display_content(s) or ""

        card = tk.Frame(parent, bg=BG_INPUT, cursor="hand2",
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", padx=10, pady=5)

        # 标题行
        title_row = tk.Frame(card, bg=BG_INPUT)
        title_row.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(title_row, text=s.get("title", "未命名"), bg=BG_INPUT, fg=FG,
                 font=(FONT, 13, "bold"), anchor="w").pack(side="left", fill="x", expand=True)

        # 内容预览（3 行左右）
        clean = re.sub(r"@image#\d+:[^\s]+", "[图]", content) if content else ""
        preview = clean[:180].strip()
        if len(clean) > 180:
            preview += "..."
        tk.Label(card, text=preview, bg=BG_INPUT, fg=FG_DIM,
                 font=(FONT, 10), anchor="w", justify="left",
                 wraplength=680).pack(fill="x", padx=16, pady=(0, 10))

        # 底部：标签 chips + 时间
        footer = tk.Frame(card, bg=BG_INPUT)
        footer.pack(fill="x", padx=16, pady=(0, 12))

        # 分类名（左侧带图标）
        cat_name = category_name(self.data, s.get("category_id")) or "未分类"
        tk.Label(footer, text="◈ " + cat_name, bg=BG_INPUT, fg=FG_DIM,
                 font=(FONT, 9), padx=6, pady=2).pack(side="left")

        # 标签 chips
        for tid in s.get("tag_ids", []):
            tname = tag_names_map.get(tid)
            if not tname:
                continue
            is_active = tid in self.active_tags
            chip_bg = ACCENT if is_active else BG
            chip_fg = BG if is_active else FG_DIM
            chip = tk.Label(footer, text=tname, bg=chip_bg, fg=chip_fg,
                            font=(FONT, 9), padx=8, pady=2, cursor="hand2")
            chip.pack(side="left", padx=(4, 0))
            chip.bind("<Button-1>", lambda e, t=tid: self._toggle_tag_filter(t))

        # 时间（右侧）
        upd = self._fmt_time(s.get("updated_at") or s.get("created_at"))
        if upd:
            tk.Label(footer, text="⏱ " + upd, bg=BG_INPUT, fg=FG_DIM,
                     font=(FONT, 9)).pack(side="right")

        # 卡片整体点击 → 进入编辑；右键 → 上下文菜单
        def on_click(e, sn=s):
            self._edit_snippet(sn)
        def on_right(e, sn=s):
            self._show_card_menu(e, sn)

        # 只在非 chip 区域绑定，避免和 chip 点击冲突
        card.bind("<Button-1>", on_click)
        card.bind("<Button-3>", on_right)
        title_row.bind("<Button-1>", on_click)
        title_row.bind("<Button-3>", on_right)
        for w in title_row.winfo_children():
            w.bind("<Button-1>", on_click)
            w.bind("<Button-3>", on_right)

        def on_enter(e, c=card): c.configure(bg=BG_HOVER, highlightbackground=ACCENT)
        def on_leave(e, c=card): c.configure(bg=BG_INPUT, highlightbackground=BORDER)
        card.bind("<Enter>", on_enter)
        card.bind("<Leave>", on_leave)

    def _filter(self):
        if not hasattr(self, "tree"):
            return
        self._clear_tag_view()
        # 标签焦点模式：走卡片视图（模仿得到大脑）
        if self.active_tags and not self.active_category_id:
            if hasattr(self, "subcat_bar"):
                self.subcat_bar.pack_forget()
            self._render_tag_view()
            return
        # 常态：所有分类（叶/父）都走列表视图 + 顶部 chip 跳转条
        self._show_list_view()
        self._render_subcat_bar(self.active_category_id)
        self.tree.delete(*self.tree.get_children())
        q = self.search_var.get().lower().strip() if hasattr(self, "search_var") else ""
        tag_names = tag_id_to_name(self.data)
        cat_ids = collect_category_descendants(self.data, self.active_category_id) if self.active_category_id else None
        self.filtered = []
        for s in self._sorted(self.snippets):
            if s.get("_deleted"):
                continue
            title = s.get("title", "")
            content = prompt_display_content(s)
            if cat_ids and s.get("category_id") not in cat_ids:
                continue
            if q and q not in title.lower() and q not in (content or "").lower():
                continue
            tag_ids = set(s.get("tag_ids", []))
            if self.active_tags and not self.active_tags.issubset(tag_ids):
                continue
            self.filtered.append(s)
            tags = [tag_names.get(tid, "") for tid in s.get("tag_ids", [])]
            tag_str = " ".join(f"#{t}" for t in tags if t)
            # 标题截断：列宽 260px @ 12pt 约容纳 18 个中文字
            title_display = title[:24] + ("..." if len(title) > 24 else "")
            # 预览截断：列宽 430px @ 12pt 约容纳 35 个中文字，再加标签信息
            # 先去掉行内图片占位符（@image#N:xxx.png），避免在列表里露出路径
            clean = re.sub(r"@image#\d+:[^\s]+", "[图]", content) if content else ""
            preview_text = clean.replace("\n", " ")
            preview_body = preview_text[:50] + ("..." if len(preview_text) > 50 else "")
            if tag_str:
                preview_display = f"{preview_body}  {tag_str}"
            else:
                preview_display = preview_body
            self.tree.insert("", "end", iid=s["id"], values=(title_display, preview_display))
        if hasattr(self, "count_label"):
            # 分母：当前分类（含子分类）下的条目总数，而非全局总数
            if self.active_category_id:
                total = self._category_prompt_count(self.active_category_id)
                label = f"{category_name(self.data, self.active_category_id)}"
            else:
                total = len(self.snippets)
                label = "全部"

            showing = len(self.filtered)

            # 筛选原因：告诉用户为什么 showing ≠ total
            q = self.search_var.get().strip() if hasattr(self, "search_var") else ""
            has_search = bool(q)
            has_tags = bool(self.active_tags)

            if has_search and has_tags:
                suffix = f"（筛选后 {showing}）"
            elif has_search:
                suffix = f"（搜索\"{q[:12]}\" 匹配 {showing}）"
            elif has_tags:
                suffix = f"（标签筛选 {showing}）"
            elif showing != total:
                # 无主动筛选但数量不对 → 子分类中的条目被隐藏，不额外解释
                suffix = ""
            else:
                suffix = ""

            if suffix:
                self.count_label.config(text=f"{label} · {total} 条{suffix}")
            else:
                self.count_label.config(text=f"{label} · {total} 条")
        if self.filtered:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
        else:
            self.selected = None
            self._clear_frames()

    def _move(self, d):
        ch = self.tree.get_children()
        if not ch:
            return "break"
        cur = self.tree.focus()
        idx = list(ch).index(cur) if cur else 0
        new = max(0, min(len(ch)-1, idx+d))
        self.tree.selection_set(ch[new])
        self.tree.focus(ch[new])
        return "break"

    def _selected_from_tree(self):
        sel = self.tree.selection()
        if not sel:
            return None
        sid = sel[0]
        return next((s for s in self.filtered if s.get("id") == sid), None)

    def _on_drag_start(self, event):
        """按压条目，记录起始位置和 snippet。"""
        # 确定被点击的行
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        snippet = next((s for s in self.filtered if s.get("id") == row_id), None)
        if not snippet:
            return
        self._drag_snippet = snippet
        self._drag_start = (event.x_root, event.y_root)
        self._drag_active = False
        self._drag_preview = None
        # 选中被拖条目
        self.tree.selection_set(row_id)

    def _on_drag_motion(self, event):
        """检测拖曳阈值并激活拖曳模式。"""
        if self._drag_snippet is None or self._drag_start is None:
            return
        dx = event.x_root - self._drag_start[0]
        dy = event.y_root - self._drag_start[1]
        if not self._drag_active:
            if abs(dx) + abs(dy) < 10:
                return
            self._drag_active = True
            self.win.config(cursor="fleur")
            self._draw_drag_preview(event)

        # 更新拖曳预览位置
        self._update_drag_preview(event)

        # 高亮悬停的分类
        self._highlight_drop_target(event)

    def _on_drag_release(self, event):
        """释放拖曳：如果悬停在有效分类上则移动条目。"""
        snippet = self._drag_snippet
        active = self._drag_active
        self._cancel_drag()
        if snippet is None or not active:
            return
        # 只在真正拖曳后（非单击）才处理 drop
        target_cat = self._category_under_cursor(event.x_root, event.y_root)
        if target_cat and target_cat != snippet.get("category_id"):
            snippet["category_id"] = target_cat
            snippet["updated_at"] = now()
            self._save_data()
            self._render_categories()
            self._filter()

    def _cancel_drag(self):
        """安全退出拖曳模式，清理所有视觉效果。"""
        self._drag_snippet = None
        self._drag_start = None
        self._drag_active = False
        if self.win and self.win.winfo_exists():
            self.win.config(cursor="")
        self._clear_drag_highlight()
        self._clear_drag_preview()
        self._drag_preview = None

    def _category_under_cursor(self, root_x, root_y):
        """根据屏幕坐标判断鼠标是否在左侧分类树的有效行上。"""
        if not self.cat_tree.winfo_exists():
            return None
        # 屏幕坐标 → 控件相对坐标
        try:
            rx = root_x - self.cat_tree.winfo_rootx()
            ry = root_y - self.cat_tree.winfo_rooty()
            row_id = self.cat_tree.identify_row(ry)
            if row_id:
                return row_id
        except Exception:
            pass
        return None

    def _highlight_drop_target(self, event):
        """拖曳过程中高亮鼠标悬停的分类行。"""
        self._clear_drag_highlight()
        target = self._category_under_cursor(event.x_root, event.y_root)
        if target:
            self.cat_tree.tag_configure("drop", background=ACCENT, foreground=BG)
            self.cat_tree.item(target, tags=("drop",))

    def _clear_drag_highlight(self):
        """清除所有分类树上的拖曳高亮。"""
        for item in self.cat_tree.get_children():
            self._clear_item_highlight(item)

    def _clear_item_highlight(self, item):
        """递归清除单个节点的高亮标签。"""
        self.cat_tree.item(item, tags=())
        for child in self.cat_tree.get_children(item):
            self._clear_item_highlight(child)

    def _draw_drag_preview(self, event):
        """用 Canvas 画一条插入参考线（简化版：跟随鼠标的光标 + 色块）。"""
        self._clear_drag_preview()
        self._drag_preview = tk.Label(
            self.win,
            text=f"📋 {self._drag_snippet.get('title','')[:20]}",
            bg=ACCENT, fg=BG,
            font=(FONT, 11, "bold"),
            padx=10, pady=4,
        )
        self._drag_preview.place(x=event.x, y=event.y - 20)

    def _update_drag_preview(self, event):
        """更新拖曳预览标签的位置。"""
        if self._drag_preview and self._drag_preview.winfo_exists():
            self._drag_preview.place(x=event.x, y=event.y - 20)

    def _clear_drag_preview(self):
        """销毁拖曳预览控件。"""
        if self._drag_preview and self._drag_preview.winfo_exists():
            self._drag_preview.destroy()
        self._drag_preview = None

    def _on_select(self):
        s = self._selected_from_tree()
        if not s:
            return
        self.selected = s
        content = prompt_display_content(s)
        phs = extract_placeholders(content or "")
        self._clear_frames()
        if phs:
            self._show_ph_actions(s, phs)
        else:
            self._show_actions(s)

    def _on_enter(self):
        s = self._selected_from_tree()
        if not s:
            return
        content = prompt_display_content(s)
        phs = extract_placeholders(content or "")
        if phs:
            self.selected = s
            self._clear_frames()
            self._show_ph_actions(s, phs)
        else:
            self._do_copy(content or "")

    def _on_double(self):
        s = self._selected_from_tree()
        if not s:
            return
        self._edit_snippet(s)

    def _clear_frames(self):
        if hasattr(self, "action_frame"):
            for w in self.action_frame.winfo_children():
                w.destroy()
        if hasattr(self, "ph_frame"):
            for w in self.ph_frame.winfo_children():
                w.destroy()
        self.ph_entries = {}
        self.ph_definitions = {}

    def _snippet_tag_names(self, s):
        names = tag_id_to_name(self.data)
        return [names[tid] for tid in s.get("tag_ids", []) if tid in names]

    def _show_actions(self, s):
        tk.Label(self.action_frame, text=f"已选：{s['title']}", bg=BG, fg=ACCENT,
                 font=(FONT, 12, "bold")).pack(side="left", padx=(0, 10))
        ver_label = snippet_version_label(s)
        tk.Label(self.action_frame, text=ver_label, bg=ACCENT_2, fg=BG,
                 font=(FONT, 9, "bold"), padx=6, pady=1).pack(side="left", padx=(0, 10))
        for name in self._snippet_tag_names(s):
            tk.Label(self.action_frame, text=f"#{name}", bg=BG_INPUT, fg=FG_DIM,
                     font=(FONT, 9), padx=6, pady=1).pack(side="left", padx=(0, 4))
        # 变体来源/派生信息
        self._show_variant_info(s)
        content = prompt_display_content(s)
        copy_btn = self._btn(self.action_frame, "复制", None, ACCENT, BG, 12, True)
        copy_btn.config(command=lambda: self._do_copy(content, copy_btn))
        copy_btn.pack(side="right", padx=2)
        self._btn(self.action_frame, "粘贴", lambda: self._do_paste(content), BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right", padx=2)
        self._btn(self.action_frame, "评分", lambda: self._show_run_dialog(s), BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "变体", lambda: self._show_variant_dialog(s), BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "历史", lambda: self._show_version_history(s), ACCENT_2, BG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "编辑", lambda: self._edit_snippet(s), BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "删除", lambda: self._del_snippet(s), BTN_DANGER, BTN_DANGER_FG, 11).pack(side="right", padx=2)

    def _show_ph_actions(self, s, phs):
        self._show_actions_header_for_ph(s)
        tk.Label(self.ph_frame, text="填写变量：", bg=BG, fg=ACCENT, font=(FONT, 11, "bold")).pack(anchor="w", pady=(2, 6))
        content = prompt_display_content(s)
        template = PromptTemplate.from_text(content, s.get("variable_definitions", {}))
        self.ph_definitions = template.definitions()
        for variable in template.variables:
            ph = variable["name"]
            row = tk.Frame(self.ph_frame, bg=BG)
            row.pack(fill="x", pady=3)
            label_text = f"{{{ph}}}"
            if variable["description"]:
                label_text += f"：{variable['description']}"
            if variable["example"]:
                label_text += f"（例如：{variable['example']}）"
            tk.Label(row, text=label_text, bg=BG, fg=WARN, font=(FONT, 12), width=42, anchor="w").pack(side="left")
            e = tk.Entry(row, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 12), bd=0, highlightthickness=0,
                         selectbackground=ACCENT, selectforeground=BG)
            e.pack(side="left", fill="x", expand=True, ipady=5, padx=(6, 0))
            self.ph_entries[ph] = e
        self.ph_entries[phs[0]].focus_set()

    def _show_actions_header_for_ph(self, s):
        tk.Label(self.action_frame, text=f"已选：{s['title']}", bg=BG, fg=ACCENT,
                 font=(FONT, 12, "bold")).pack(side="left", padx=(0, 10))
        ver_label = snippet_version_label(s)
        tk.Label(self.action_frame, text=ver_label, bg=ACCENT_2, fg=BG,
                 font=(FONT, 9, "bold"), padx=6, pady=1).pack(side="left", padx=(0, 10))
        content = prompt_display_content(s)
        copy_btn = self._btn(self.action_frame, "填充并复制", None, ACCENT, BG, 12, True)
        copy_btn.config(command=lambda: self._fill_copy(s, copy_btn))
        copy_btn.pack(side="right", padx=2)
        self._btn(self.action_frame, "填充并粘贴", lambda: self._fill_paste(s), BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right", padx=2)
        self._btn(self.action_frame, "快照", lambda: self._show_snapshot_history(s), ACCENT_2, BG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "历史", lambda: self._show_version_history(s), ACCENT_2, BG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "编辑", lambda: self._edit_snippet(s), BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=2)
        self._btn(self.action_frame, "删除", lambda: self._del_snippet(s), BTN_DANGER, BTN_DANGER_FG, 11).pack(side="right", padx=2)

    def _fill(self, s):
        content = prompt_display_content(s)
        template = PromptTemplate.from_text(content, s.get("variable_definitions", {}))
        values = {variable["name"]: self.ph_entries[variable["name"]].get() for variable in template.variables}
        return template.render(values)

    def _capture_variable_snapshot(self, s, trigger, rendered_prompt):
        content = prompt_display_content(s)
        template = PromptTemplate.from_text(content, s.get("variable_definitions", {}))
        values = {variable["name"]: self.ph_entries[variable["name"]].get() for variable in template.variables}
        snapshot = create_snapshot(
            snippet_id=s.get("id", ""),
            version_id=s.get("current_version_id", ""),
            trigger=trigger,
            template=content,
            variable_definitions=s.get("variable_definitions", {}),
            variables=values,
            rendered_prompt=rendered_prompt,
        )
        append_snapshot(s, snapshot)
        self._save_data()
        return snapshot

    def _do_copy(self, t, btn=None):
        copy_to_clipboard(t)
        if btn:
            # 视觉反馈，按钮显示 "✓ 已复制" 并禁用/变绿
            orig_text = btn.cget("text")
            orig_bg = btn.cget("bg")
            btn.config(text="✓ 已复制", bg=ACCENT_2, state="disabled")
            def reset():
                try:
                    btn.config(text=orig_text, bg=orig_bg, state="normal")
                except Exception:
                    pass
            self.win.after(1200, reset)
        else:
            # 兜底轻量级消息提示，防止在无按钮环境下不知道已复制
            pass

    def _do_paste(self, t):
        do_paste(t)
        self._close()

    def _fill_copy(self, s, btn=None):
        rendered = self._fill(s)
        self._capture_variable_snapshot(s, "填充并复制", rendered)
        self._do_copy(rendered, btn)

    def _fill_paste(self, s):
        rendered = self._fill(s)
        self._capture_variable_snapshot(s, "填充并粘贴", rendered)
        self._do_paste(rendered)

    def _show_snapshot_history(self, snippet):
        snapshots = get_snapshots(snippet)
        dlg = tk.Toplevel(self.win)
        dlg.title(f"变量快照 · {snippet.get('title', '')}")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("760x520")
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        dlg.geometry(f"760x520+{(sw-760)//2}+{(sh-520)//2}")

        tk.Label(
            dlg,
            text=f"「{snippet.get('title', '')}」共 {len(snapshots)} 条快照",
            bg=BG,
            fg=FG,
            font=(FONT, 14, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 10))
        list_frame = tk.Frame(dlg, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG)
        scroll_frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        if not snapshots:
            tk.Label(scroll_frame, text="还没有保存过变量快照。", bg=BG, fg=FG_DIM, font=(FONT, 11)).pack(anchor="w", pady=12)
        for snapshot in snapshots:
            card = tk.Frame(scroll_frame, bg=BG_PANEL, padx=10, pady=8)
            card.pack(fill="x", pady=(0, 8))
            title = f"{snapshot.get('created_at', '')} · {snapshot.get('trigger', '')}"
            tk.Label(card, text=title, bg=BG_PANEL, fg=ACCENT, font=(FONT, 10, "bold")).pack(anchor="w")
            preview = snapshot.get("rendered_prompt", "")
            tk.Label(card, text=preview[:160] + ("..." if len(preview) > 160 else ""), bg=BG_PANEL, fg=FG, justify="left", anchor="w", wraplength=650).pack(fill="x", pady=(4, 6))
            values = "；".join(f"{key}={value}" for key, value in snapshot.get("variables", {}).items())
            tk.Label(card, text=f"变量：{values or '无'}", bg=BG_PANEL, fg=FG_DIM, justify="left", anchor="w", wraplength=650).pack(fill="x")
            self._btn(card, "复制这条快照", lambda text=preview: copy_to_clipboard(text), BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(anchor="e", pady=(6, 0))

        self._btn(dlg, "关闭", dlg.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(anchor="e", padx=18, pady=(0, 14))

    def _edit(self):
        s = self._selected_from_tree()
        if s:
            self._edit_snippet(s)

    def _edit_snippet(self, s):
        self._editor(s)

    def _rename_snippet(self, s):
        new_title = self._ask_string(
            "重命名提示词", "新标题：", initialvalue=s.get("title", "")
        )
        if new_title is None:
            return
        new_title = new_title.strip()
        if not new_title or new_title == s.get("title", ""):
            return
        s["title"] = new_title
        s["updated_at"] = now()
        self._save_data()
        self._filter()

    def _snippet_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self._on_select()
        s = self._selected_from_tree()
        menu = tk.Menu(self.win, tearoff=0, bg=BG_PANEL, fg=FG)
        if s is not None:
            menu.add_command(label="重命名提示词", command=lambda: self._rename_snippet(s))
            menu.add_command(label="编辑", command=lambda: self._edit_snippet(s))
            menu.add_command(label="删除", command=lambda: self._del_snippet(s))
        else:
            menu.add_command(label="新建提示词", command=self._add)
        menu.tk_popup(event.x_root, event.y_root)

    def _delete(self):
        s = self._selected_from_tree()
        if s:
            self._del_snippet(s)

    def _del_snippet(self, s):
        if messagebox.askyesno("删除", f"确定删除「{s['title']}」？\n可从数据文件中恢复。", parent=self.win):
            s["_deleted"] = True
            s["updated_at"] = now()
            self._save_data()
            self._render_categories()
            self._update_tags()
            self._clear_frames()
            self._filter()

    def _add(self):
        self._editor(None)

    def _ensure_tag(self, name):
        name = str(name).strip().lstrip("#")
        if not name:
            return None
        by_name = tag_name_to_id(self.data)
        if name in by_name:
            return by_name[name]
        tid = make_id("tag", name)
        existing = {t["id"] for t in self.data["tags"]}
        n = 2
        original = tid
        while tid in existing:
            tid = f"{original}_{n}"
            n += 1
        self.data["tags"].append({"id": tid, "name": name})
        return tid

    def _editor(self, snippet):
        is_new = snippet is None
        dlg = tk.Toplevel(self.win or self.root)
        dlg.title("新建提示词" if is_new else "编辑提示词")
        apply_window_icon(dlg)
        dlg.lift()
        dlg.focus_force()
        dlg.configure(bg=BG)
        dlg.geometry("780x680")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        x = (sw - 780) // 2
        y = (sh - 680) // 2
        dlg.geometry(f"780x680+{x}+{y}")

        def on_close():
            if dlg._has_edited:
                if not messagebox.askyesno("放弃编辑", "有未保存的内容，确定关闭？", parent=dlg):
                    return
            dlg.destroy()

        dlg._has_edited = False
        dlg.protocol("WM_DELETE_WINDOW", on_close)

        tk.Label(dlg, text="标题", bg=BG, fg=FG, font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(16, 5))
        title_e = tk.Entry(dlg, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 14), bd=0, highlightthickness=0,
                           selectbackground=ACCENT, selectforeground=BG)
        title_e.pack(fill="x", padx=18, ipady=7)
        title_e.insert(0, snippet["title"] if snippet else "")

        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=18, pady=(10, 5))
        tk.Label(row, text="分类", bg=BG, fg=FG, font=(FONT, 12, "bold")).pack(side="left")
        cat_var = tk.StringVar()
        cats = self.data.get("categories", [])
        cat_options = []
        for c in cats:
            indent = "  " if c.get("parent_id") else ""
            cat_options.append(f"{indent}{c['name']}|{c['id']}")
        current_cat = snippet.get("category_id") if snippet else (self.active_category_id or DEFAULT_CATEGORY_ID)
        display = next((x for x in cat_options if x.endswith("|" + current_cat)), cat_options[0] if cat_options else f"{DEFAULT_CATEGORY_NAME}|{DEFAULT_CATEGORY_ID}")
        cat_var.set(display)
        cat_menu = tk.OptionMenu(row, cat_var, *cat_options)
        cat_menu.configure(bg=BG_INPUT, fg=FG, activebackground=BG_HOVER, activeforeground=FG, relief="flat", bd=0, font=(FONT, 10))
        cat_menu["menu"].configure(bg=BG_PANEL, fg=FG)
        cat_menu.pack(side="left", padx=(10, 0))

        tk.Label(dlg, text="标签", bg=BG, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(10, 5))
        tag_panel = tk.Frame(dlg, bg=BG)
        tag_panel.pack(fill="x", padx=18)
        selected_tag_ids = set(snippet.get("tag_ids", [])) if snippet else set()

        def render_tag_panel():
            for w in tag_panel.winfo_children():
                w.destroy()
            for tag in sorted(self.data.get("tags", []), key=lambda t: t["name"]):
                active = tag["id"] in selected_tag_ids
                label = tk.Label(tag_panel, text=("✓ " if active else "+ ") + tag["name"],
                                 bg=ACCENT if active else BG_INPUT, fg=BG if active else FG_DIM,
                                 font=(FONT, 10), padx=8, pady=3, cursor="hand2")
                label.pack(side="left", padx=(0, 5), pady=(0, 5))
                label.bind("<Button-1>", lambda e, tid=tag["id"]: toggle_tag(tid))

        def toggle_tag(tid):
            if tid in selected_tag_ids:
                selected_tag_ids.remove(tid)
            else:
                selected_tag_ids.add(tid)
            render_tag_panel()

        def smart_tags():
            title = title_e.get().strip()
            content = txt.get("1.0", "end-1c").strip()
            if not title and not content:
                messagebox.showinfo("智能标签", "先填写标题或内容再打标签。", parent=dlg)
                return
            existing = [t["name"] for t in self.data.get("tags", [])]
            recs = recommend_tags(title, content, existing, limit=5)
            if not recs:
                messagebox.showinfo("智能标签", "没有找到合适标签。", parent=dlg)
                return
            msg = "本地推荐标签：\n" + "、".join(recs) + "\n\n是否添加到当前提示词？"
            if messagebox.askyesno("智能标签", msg, parent=dlg):
                for name in recs:
                    tid = self._ensure_tag(name)
                    if tid:
                        selected_tag_ids.add(tid)
                render_tag_panel()

        def copy_llm_prompt():
            """把打标签的提示词复制到剪贴板，桥接给外部 AI (Claude/GPT/WorkBuddy)"""
            title = title_e.get().strip()
            content = txt.get("1.0", "end-1c").strip()
            if not title and not content:
                messagebox.showinfo("交给 AI 打标签", "先填写标题或内容。", parent=dlg)
                return
            existing = [t["name"] for t in self.data.get("tags", [])]
            prompt = build_smart_tag_prompt(title, content, existing, limit=5)
            try:
                pyperclip.copy(prompt)
                messagebox.showinfo(
                    "交给 AI 打标签",
                    "打标签提示词已复制到剪贴板。\n\n"
                    "粘贴到 Claude / GPT / WorkBuddy 得到标签数组，\n"
                    "再回到这里点“+ 新标签”手动添加。",
                    parent=dlg,
                )
            except Exception as e:
                messagebox.showerror("复制失败", str(e), parent=dlg)

        tag_actions = tk.Frame(dlg, bg=BG)
        tag_actions.pack(fill="x", padx=18, pady=(0, 5))
        self._btn(tag_actions, "+ 新标签", lambda: add_custom_tag(), BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left", padx=(0, 6))
        self._btn(tag_actions, "本地推荐", smart_tags, ACCENT_2, BG, 10, True).pack(side="left", padx=(0, 6))
        self._btn(tag_actions, "交给 AI 打标签", copy_llm_prompt, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left")

        def add_custom_tag():
            name = self._ask_string("新标签", "标签名称：", parent=dlg)
            if not name:
                return
            tid = self._ensure_tag(name)
            if tid:
                selected_tag_ids.add(tid)
            render_tag_panel()

        render_tag_panel()

        # ── 业务上下文（可选）：写 Prompt 时即可注入，不必等"失灵"才进工作台 ──
        ctx_frame = tk.LabelFrame(dlg, text="业务上下文（可选，随提示词保存）", bg=BG, fg=FG_DIM,
                                  font=(FONT, 10, "bold"), bd=0, highlightthickness=1,
                                  highlightbackground=BORDER)
        ctx_frame.pack(fill="x", padx=18, pady=(4, 0))
        ctx_btns = tk.Frame(ctx_frame, bg=BG)
        ctx_btns.pack(fill="x", padx=6, pady=(4, 2))
        ctx_text = tk.Text(ctx_frame, height=3, bg=BG_INPUT, fg=FG, insertbackground=FG,
                           relief="flat", font=(FONT, 10), bd=0, highlightthickness=0,
                           wrap="word", padx=6, pady=4, selectbackground=ACCENT, selectforeground=BG)
        ctx_text.pack(fill="x", padx=6, pady=(0, 2))
        ctx_info_var = tk.StringVar(value="")
        tk.Label(ctx_frame, textvariable=ctx_info_var, bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).pack(anchor="w", padx=6, pady=(0, 4))
        init_ctx = snippet.get("context", "") if snippet else ""
        if init_ctx:
            ctx_text.insert("1.0", init_ctx)

        def refresh_ctx_info(*_):
            content = ctx_text.get("1.0", "end-1c").strip()
            if not content:
                ctx_info_var.set("未载入上下文")
                return
            tokens = round(len(content) / 1.6)
            ctx_info_var.set(f"已载入 {len(content):,} 字符（约 {tokens:,} token）· 超过 20,000 字符将预警")

        def ctx_load_file():
            from tkinter import filedialog
            path = filedialog.askopenfilename(parent=dlg)
            if not path:
                return
            try:
                from promptbox_mvp.context_loader import load_context_file
                loaded = load_context_file(path)
            except ValueError as exc:
                messagebox.showerror("业务上下文", str(exc), parent=dlg)
                return
            ctx_text.delete("1.0", tk.END)
            ctx_text.insert("1.0", loaded["text"])
            if loaded["note"]:
                messagebox.showinfo("业务上下文", loaded["note"], parent=dlg)
            refresh_ctx_info()

        def ctx_load_clipboard():
            if pyperclip is None:
                messagebox.showerror("业务上下文", "未安装 pyperclip，无法读取剪贴板。", parent=dlg)
                return
            try:
                content = pyperclip.paste()
            except Exception as exc:
                messagebox.showerror("业务上下文", f"读取剪贴板失败：{exc}", parent=dlg)
                return
            if not content.strip():
                messagebox.showinfo("业务上下文", "剪贴板是空的。", parent=dlg)
                return
            ctx_text.delete("1.0", tk.END)
            ctx_text.insert("1.0", content)
            refresh_ctx_info()

        self._btn(ctx_btns, "选择文件", ctx_load_file, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left", padx=(0, 4))
        self._btn(ctx_btns, "载入剪贴板", ctx_load_clipboard, BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left", padx=(0, 4))
        self._btn(ctx_btns, "清空", lambda: (ctx_text.delete("1.0", tk.END), refresh_ctx_info()), BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left")
        ctx_text.bind("<KeyRelease>", refresh_ctx_info)
        refresh_ctx_info()

        tk.Label(dlg, text="提示词内容（支持 {变量}）", bg=BG, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(8, 5))

        def mark_edited(*_):
            dlg._has_edited = True
        title_e.bind("<Key>", mark_edited)

        # ── 版本变更说明（编辑现有提示词时显示）──
        changelog_frame = tk.Frame(dlg, bg=BG)
        changelog_var = tk.StringVar()
        if not is_new:
            changelog_frame.pack(fill="x", padx=18, pady=(0, 8))
            tk.Label(changelog_frame, text="版本说明", bg=BG, fg=FG_DIM,
                     font=(FONT, 11, "bold")).pack(anchor="w")
            changelog_row = tk.Frame(changelog_frame, bg=BG)
            changelog_row.pack(fill="x", pady=(4, 0))
            changelog_e = tk.Entry(changelog_row, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                                   font=(FONT, 11), bd=0, highlightthickness=0,
                                   selectbackground=ACCENT, selectforeground=BG, textvariable=changelog_var)
            changelog_e.pack(side="left", fill="x", expand=True, ipady=4)
            tk.Label(changelog_row, text="可跳过", bg=BG, fg=FG_DIM,
                     font=(FONT, 9)).pack(side="right", padx=(6, 0))

        # 编辑器
        tf = tk.Frame(dlg, bg=BG)
        tf.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=18, pady=(8, 16), side="bottom")

        ph_lbl = tk.Label(dlg, text="", bg=BG, fg=FG_DIM, font=(FONT, 10))
        ph_lbl.pack(anchor="w", padx=18, side="bottom")

        # ── Markdown 点选工具栏（方案 C，纯函数层在 promptbox_mvp.md_toolbar）──
        from promptbox_mvp.md_toolbar import (
            EditResult,
            insert_fence,
            insert_placeholder,
            insert_rule,
            insert_table,
            insert_task,
            toggle_blockquote,
            toggle_heading,
            toggle_list,
            wrap_selection,
        )

        def _tk_index_to_char(index: str) -> int:
            line, col = map(int, index.split("."))
            lines = txt.get("1.0", "end-1c").split("\n")
            if line == 1:
                return col
            return sum(len(l) + 1 for l in lines[: line - 1]) + col

        def _char_to_tk_index(pos: int) -> str:
            content = txt.get("1.0", "end-1c")
            line = content.count("\n", 0, pos) + 1
            col = pos - (content.rfind("\n", 0, pos) + 1)
            return f"{line}.{col}"

        def _apply_edit(result: EditResult) -> None:
            txt.delete("1.0", tk.END)
            txt.insert("1.0", result.text)
            txt.mark_set(tk.INSERT, _char_to_tk_index(result.end))
            txt.see(tk.INSERT)
            mark_edited()
            update_ph()

        def _toolbar_heading(level: int) -> None:
            sel = txt.tag_ranges("sel")
            if sel:
                start = _tk_index_to_char(str(sel[0]))
                end = _tk_index_to_char(str(sel[1]))
            else:
                start = end = _tk_index_to_char(str(txt.index(tk.INSERT)))
            _apply_edit(toggle_heading(txt.get("1.0", "end-1c"), start, end, level))

        def _toolbar_wrap(token: str) -> None:
            sel = txt.tag_ranges("sel")
            if sel:
                start = _tk_index_to_char(str(sel[0]))
                end = _tk_index_to_char(str(sel[1]))
            else:
                start = end = _tk_index_to_char(str(txt.index(tk.INSERT)))
            _apply_edit(wrap_selection(txt.get("1.0", "end-1c"), start, end, token))

        def _toolbar_line_op(fn) -> None:
            sel = txt.tag_ranges("sel")
            if sel:
                start = _tk_index_to_char(str(sel[0]))
                end = _tk_index_to_char(str(sel[1]))
            else:
                start = end = _tk_index_to_char(str(txt.index(tk.INSERT)))
            _apply_edit(fn(txt.get("1.0", "end-1c"), start, end))

        def _toolbar_table() -> None:
            dialog = tk.Toplevel(dlg)
            dialog.title("插入表格")
            dialog.configure(bg=BG)
            dialog.transient(dlg)
            dialog.grab_set()
            rows_var = tk.StringVar(value="3")
            cols_var = tk.StringVar(value="3")
            tk.Label(dialog, text="行数", bg=BG, fg=FG, font=(FONT, 11)).grid(row=0, column=0, padx=8, pady=(10, 2))
            tk.Entry(dialog, textvariable=rows_var, width=6, bg=BG_INPUT, fg=FG,
                     insertbackground=FG, relief="flat", font=(FONT, 11)).grid(row=0, column=1, padx=8)
            tk.Label(dialog, text="列数", bg=BG, fg=FG, font=(FONT, 11)).grid(row=1, column=0, padx=8, pady=2)
            tk.Entry(dialog, textvariable=cols_var, width=6, bg=BG_INPUT, fg=FG,
                     insertbackground=FG, relief="flat", font=(FONT, 11)).grid(row=1, column=1, padx=8)

            def confirm() -> None:
                try:
                    rows = int(rows_var.get())
                    cols = int(cols_var.get())
                except ValueError:
                    messagebox.showwarning("插入表格", "行数与列数必须是数字。", parent=dialog)
                    return
                try:
                    sel = txt.tag_ranges("sel")
                    if sel:
                        start = _tk_index_to_char(str(sel[0]))
                        end = _tk_index_to_char(str(sel[1]))
                    else:
                        start = end = _tk_index_to_char(str(txt.index(tk.INSERT)))
                    _apply_edit(insert_table(txt.get("1.0", "end-1c"), start, end, rows, cols))
                except ValueError as exc:
                    messagebox.showwarning("插入表格", str(exc), parent=dialog)
                    return
                dialog.destroy()

            tk.Button(dialog, text="插入", command=confirm, bg=ACCENT, fg=BG,
                      font=(FONT, 10)).grid(row=2, column=0, columnspan=2, pady=10)
            dialog.update_idletasks()
            w = dialog.winfo_reqwidth()
            h = dialog.winfo_reqheight()
            x = dlg.winfo_rootx() + (dlg.winfo_width() - w) // 2
            y = dlg.winfo_rooty() + (dlg.winfo_height() - h) // 2
            dialog.geometry(f"+{x}+{y}")

        tb = tk.Frame(tf, bg=BG)
        tb.pack(side="top", fill="x", pady=(0, 4))
        style_args = dict(bg=BG_PANEL, fg=FG, font=(FONT, 9), bd=0,
                          highlightthickness=1, highlightbackground=BG_HOVER,
                          activebackground=BG_HOVER, activeforeground=FG, padx=6, pady=1)
        for label, handler in [
            ("大标题", lambda: _toolbar_heading(1)),
            ("中标题", lambda: _toolbar_heading(2)),
            ("小标题", lambda: _toolbar_heading(3)),
            ("加粗", lambda: _toolbar_wrap("**")),
            ("斜体", lambda: _toolbar_wrap("*")),
            ("行内代码", lambda: _toolbar_wrap("`")),
            ("引用", lambda: _toolbar_line_op(toggle_blockquote)),
            ("代码块", lambda: _toolbar_line_op(lambda t, s, e: insert_fence(t, s, e))),
            ("分隔线", lambda: _toolbar_line_op(insert_rule)),
            ("列表", lambda: _toolbar_line_op(toggle_list)),
            ("编号列表", lambda: _toolbar_line_op(lambda t, s, e: toggle_list(t, s, e, ordered=True))),
            ("任务框", lambda: _toolbar_line_op(insert_task)),
            ("表格", _toolbar_table),
            ("变量", lambda: _toolbar_line_op(insert_placeholder)),
        ]:
            tk.Button(tb, text=label, command=handler, **style_args).pack(side="left", padx=(0, 3))

        txt = tk.Text(tf, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat",
                      font=(FONT, 13), bd=0, highlightthickness=0, wrap="word", padx=8, pady=8,
                      selectbackground=ACCENT, selectforeground=BG)
        txt.bind("<Key>", mark_edited)
        scr = ttk.Scrollbar(tf, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")
        # 编辑时加载当前版本内容
        init_content = prompt_display_content(snippet) if snippet else ""
        txt.insert("1.0", init_content)

        def update_ph(*args):
            phs = extract_placeholders(txt.get("1.0", "end-1c"))
            ph_lbl.config(text=f"可替换项: {', '.join('{'+p+'}' for p in phs)}" if phs else "")
        txt.bind("<KeyRelease>", update_ph)

        variable_frame = tk.LabelFrame(dlg, text="变量说明和示例（可选）", bg=BG, fg=FG_DIM,
                                       font=(FONT, 10, "bold"), bd=0, highlightthickness=1,
                                       highlightbackground=BORDER)
        variable_frame.pack(fill="x", padx=18, pady=(0, 6))
        variable_fields = {}

        def refresh_variable_fields(*_args):
            for child in variable_frame.winfo_children():
                child.destroy()
            variable_fields.clear()
            template = PromptTemplate.from_text(txt.get("1.0", "end-1c"))
            old_definitions = snippet.get("variable_definitions", {}) if snippet else {}
            for variable in template.variables:
                name = variable["name"]
                old = old_definitions.get(name, {})
                row = tk.Frame(variable_frame, bg=BG)
                row.pack(fill="x", padx=6, pady=2)
                tk.Label(row, text="{" + name + "}", width=16, anchor="w", bg=BG, fg=WARN,
                         font=(FONT, 10, "bold")).pack(side="left")
                description_entry = tk.Entry(row, bg=BG_INPUT, fg=FG, relief="flat", width=24)
                description_entry.insert(0, old.get("description", ""))
                description_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
                example_entry = tk.Entry(row, bg=BG_INPUT, fg=FG, relief="flat", width=20)
                example_entry.insert(0, old.get("example", ""))
                example_entry.pack(side="left", fill="x", expand=True)
                variable_fields[name] = (description_entry, example_entry)

        def variable_rows():
            return [
                (name, description.get().strip(), example.get().strip())
                for name, (description, example) in variable_fields.items()
            ]

        txt.bind("<KeyRelease>", refresh_variable_fields, add="+")
        update_ph()
        refresh_variable_fields()

        # ── 保存逻辑 ──
        def generate_changelog():
            """产品规则2：AI 自动生成版本说明（占位，后续接 AI）"""
            old_content = prompt_display_content(snippet) if snippet else ""
            new_content = txt.get("1.0", "end-1c").strip()
            if not old_content:
                return ""
            # 简单启发式：相比旧版，统计差异量
            old_lines = len(old_content.split("\n"))
            new_lines = len(new_content.split("\n"))
            diff = new_lines - old_lines
            if diff > 3:
                return f"扩充内容（+{diff}行）"
            elif diff < -3:
                return f"精简内容（{diff}行）"
            elif any(w in new_content.lower() for w in ["不要", "禁止", "避免", "必须"]) and \
                 not any(w in old_content.lower() for w in ["不要", "禁止", "避免", "必须"]):
                return "增加约束条件"
            else:
                return "优化表达"

        def save_as_new():
            t = title_e.get().strip()
            c = txt.get("1.0", "end-1c").strip()
            variable_definitions = {
                name: {
                    "description": description,
                    "example": example,
                }
                for name, description, example in variable_rows()
            }
            if not t or not c:
                messagebox.showwarning("保存失败", "标题和内容不能为空", parent=dlg)
                return
            cat_id = cat_var.get().split("|")[-1]
            changelog = changelog_var.get().strip() if not is_new else "初始版本"
            if not changelog:
                changelog = generate_changelog()
            if is_new:
                sid = make_id("snip", t)
                existing_ids = {s["id"] for s in self.snippets}
                n = 2
                original = sid
                while sid in existing_ids:
                    sid = f"{original}_{n}"
                    n += 1
                v1_id = make_version_id()
                new_s = {
                    "id": sid,
                    "title": t,
                    "category_id": cat_id,
                    "tag_ids": list(selected_tag_ids),
                    "created_at": now(),
                    "updated_at": now(),
                    "source_prompt_id": None,
                    "source_version_id": None,
                    "scenario": "",
                    "_deleted": False,
                    "current_version_id": v1_id,
                    "stable_version_id": None,
                    "content": "",
                    "context": ctx_text.get("1.0", "end-1c").strip(),
                    "variable_definitions": variable_definitions,
                    "snapshots": [],
                    "versions": [{
                        "id": v1_id,
                        "version_number": 1,
                        "content": c,
                        "changelog": changelog,
                        "status": VER_DRAFT,
                        "created_at": now(),
                        "parent_version_id": None,
                    }],
                }
                self.snippets.append(new_s)
            else:
                snippet["variable_definitions"] = variable_definitions
                self._save_new_version(snippet, t, c, cat_id, selected_tag_ids, changelog,
                                       ctx_text.get("1.0", "end-1c").strip())
            self._save_data()
            self._render_categories()
            self._update_tags()
            self._filter()
            dlg.destroy()

        def save_overwrite():
            """覆盖当前版本内容（不创建新版）"""
            if is_new:
                save_as_new()
                return
            t = title_e.get().strip()
            c = txt.get("1.0", "end-1c").strip()
            if not t or not c:
                messagebox.showwarning("保存失败", "标题和内容不能为空", parent=dlg)
                return
            cat_id = cat_var.get().split("|")[-1]
            variable_definitions = {
                name: {
                    "description": description,
                    "example": example,
                }
                for name, description, example in variable_rows()
            }
            cur_id = snippet.get("current_version_id")
            if cur_id:
                for v in snippet.get("versions", []):
                    if v["id"] == cur_id:
                        v["content"] = c
                        v["created_at"] = now()
                        break
            snippet["title"] = t
            snippet["category_id"] = cat_id
            snippet["tag_ids"] = list(selected_tag_ids)
            snippet["variable_definitions"] = variable_definitions
            snippet["context"] = ctx_text.get("1.0", "end-1c").strip()
            snippet["updated_at"] = now()
            self._save_data()
            self._render_categories()
            self._update_tags()
            self._filter()
            dlg.destroy()

        if is_new:
            self._btn(bf, "保存", save_as_new, ACCENT, BG, 12, True).pack(side="right", padx=4)
        else:
            self._btn(bf, "保存为新版", save_as_new, ACCENT, BG, 12, True).pack(side="right", padx=4)
            self._btn(bf, "覆盖当前版", save_overwrite, BTN_SECONDARY, BTN_SECONDARY_FG, 11).pack(side="right", padx=4)
        self._btn(bf, "取消", dlg.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right", padx=4)
        title_e.focus_set()
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Control-Return>", lambda e: save_as_new())

    def _btn(self, parent, text, cmd, bg, fg, size=12, bold=False):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg, relief="flat",
                         bd=0, font=(FONT, size, "bold" if bold else "normal"),
                         cursor="hand2")

    def _close(self):
        """隐藏窗口，进程保持运行以响应下次热键。"""
        if self.win:
            self.win.destroy()
            self.win = None

    # ── 版本管理 ────────────────────────────────────────────

    def _save_new_version(self, snippet, title, content, cat_id, tag_ids, changelog="", context=""):
        """保存为新版本。"""
        versions = snippet.setdefault("versions", [])
        new_num = max((v.get("version_number", 0) for v in versions), default=0) + 1
        vid = make_version_id()
        new_ver = {
            "id": vid,
            "version_number": new_num,
            "content": content,
            "changelog": changelog or f"第{new_num}版",
            "status": VER_DRAFT,
            "created_at": now(),
            "parent_version_id": snippet.get("current_version_id"),
            "repair_case_id": None,
        }
        versions.append(new_ver)
        snippet["current_version_id"] = vid
        snippet["title"] = title
        snippet["category_id"] = cat_id
        snippet["tag_ids"] = list(tag_ids)
        snippet["context"] = context
        snippet["updated_at"] = now()
        return new_ver

    def _restore_version(self, snippet, version_id):
        """恢复历史版本：将旧版本内容复制为新版本。"""
        versions = snippet.get("versions", [])
        old_v = next((v for v in versions if v["id"] == version_id), None)
        if not old_v:
            return None
        new_num = max((v.get("version_number", 0) for v in versions), default=0) + 1
        vid = make_version_id()
        new_ver = {
            "id": vid,
            "version_number": new_num,
            "content": old_v["content"],
            "changelog": f"从 v{old_v.get('version_number')} 恢复",
            "status": VER_DRAFT,
            "created_at": now(),
            "parent_version_id": snippet.get("current_version_id"),
        }
        versions.append(new_ver)
        snippet["current_version_id"] = vid
        snippet["updated_at"] = now()
        return new_ver

    def _mark_stable(self, snippet, version_id):
        """标记稳定版。"""
        versions = snippet.get("versions", [])
        # 清除所有稳定标记
        for v in versions:
            if v.get("status") == VER_STABLE:
                v["status"] = VER_DRAFT
        # 标记新稳定版
        for v in versions:
            if v["id"] == version_id:
                v["status"] = VER_STABLE
                snippet["stable_version_id"] = version_id
                break
        snippet["updated_at"] = now()

    def _get_version_content(self, snippet, version_id):
        for v in snippet.get("versions", []):
            if v["id"] == version_id:
                return v["content"]
        return ""

    # ── 变体管理 ────────────────────────────────────────────

    def _create_variant(self, source_snippet, variant_title, scenario):
        """基于当前版本创建变体。产品规则3：scenario 必填。"""
        if not scenario or not scenario.strip():
            return None
        content = self._get_version_content(source_snippet, source_snippet.get("current_version_id", ""))
        sid = make_id("snip", variant_title)
        existing_ids = {s["id"] for s in self.snippets}
        n = 2
        original = sid
        while sid in existing_ids:
            sid = f"{original}_{n}"
            n += 1
        v1_id = make_version_id()
        new_snippet = {
            "id": sid,
            "title": variant_title,
            "category_id": source_snippet.get("category_id", DEFAULT_CATEGORY_ID),
            "tag_ids": list(source_snippet.get("tag_ids", [])),
            "created_at": now(),
            "updated_at": now(),
            "source_prompt_id": source_snippet.get("id"),
            "source_version_id": source_snippet.get("current_version_id"),
            "scenario": scenario.strip(),
            "_deleted": False,
            "current_version_id": v1_id,
            "stable_version_id": None,
            "content": "",
            "versions": [{
                "id": v1_id,
                "version_number": 1,
                "content": content,
                "changelog": f"从「{source_snippet.get('title', '')}」派生",
                "status": VER_DRAFT,
                "created_at": now(),
                "parent_version_id": None,
            }],
        }
        self.snippets.append(new_snippet)
        return new_snippet

    def _get_variants(self, snippet):
        """获取从当前提示词派生出来的所有变体。"""
        variants = []
        for s in self.snippets:
            if s.get("source_prompt_id") == snippet.get("id"):
                variants.append(s)
        return variants

    def _get_source_info(self, snippet):
        """获取当前变体的来源提示词信息。"""
        src_id = snippet.get("source_prompt_id")
        if not src_id:
            return None
        for s in self.snippets:
            if s.get("id") == src_id:
                # 找出来源版本号
                src_ver_id = snippet.get("source_version_id")
                vnum = ""
                for v in s.get("versions", []):
                    if v["id"] == src_ver_id:
                        vnum = f" v{v.get('version_number')}"
                        break
                return {"id": s["id"], "title": s["title"], "version_label": vnum}
        return None

    # ── 使用记录 ────────────────────────────────────────────

    def _add_run(self, snippet_id, version_id, rating, note="", trigger="rating", variable_snapshot_id=None):
        """记录评分或快捷调用；快捷调用不触发评分流程。"""
        created_at = now()
        run_id = make_id("run", str(int(datetime.now().timestamp() * 1_000_000)))
        if trigger == "quick_copy":
            run = build_quick_copy_run(
                snippet_id,
                version_id,
                variable_snapshot_id,
                created_at,
                run_id=run_id,
            )
        else:
            run = {
                "id": run_id,
                "snippet_id": snippet_id,
                "version_id": version_id,
                "rating": max(0, min(5, rating)),
                "note": note,
                "created_at": created_at,
            }
        self.data.setdefault("runs", []).append(run)
        return run

    def _record_quick_copy(self, snippet_id, version_id, variable_snapshot_id=None):
        """Record one successful Palette copy without prompting for a rating."""
        signature = (snippet_id, version_id, variable_snapshot_id)
        if signature == self._last_quick_copy_signature and time.monotonic() - self._last_quick_copy_at < 0.5:
            return None
        self._last_quick_copy_signature = signature
        self._last_quick_copy_at = time.monotonic()
        run = self._add_run(
            snippet_id,
            version_id,
            None,
            trigger="quick_copy",
            variable_snapshot_id=variable_snapshot_id,
        )
        self._save_data()
        return run

    def _get_runs(self, snippet_id):
        """获取某个提示词的使用记录（按时间倒序）。"""
        runs = [r for r in self.data.get("runs", []) if r.get("snippet_id") == snippet_id]
        runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return runs

    def _quick_copy_search(self, query="", sort_mode="recent"):
        """Return Palette results using shared data and current category/tag names."""
        return search_prompts(
            self.snippets,
            query,
            category_names={c.get("id"): c.get("name", "") for c in self.data.get("categories", [])},
            tag_names={t.get("id"): t.get("name", "") for t in self.data.get("tags", [])},
            runs=self.data.get("runs", []),
            sort_mode=sort_mode,
        )

    # ── 版本/变体 UI ────────────────────────────────────────

    def _show_version_history(self, snippet):
        """显示版本历史对话框。"""
        dlg = tk.Toplevel(self.win)
        dlg.title(f"版本历史 · {snippet['title']}")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("780x620")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"780x620+{(sw-780)//2}+{(sh-620)//2}")

        versions = snippet.get("versions", [])
        versions_sorted = sorted(versions, key=lambda v: v.get("version_number", 1), reverse=True)
        cur_id = snippet.get("current_version_id", "")
        stable_id = snippet.get("stable_version_id", "")

        # 顶部信息
        tk.Label(dlg, text=f"「{snippet['title']}」 共 {len(versions)} 个版本", bg=BG, fg=FG,
                 font=(FONT, 14, "bold")).pack(anchor="w", padx=18, pady=(16, 10))

        # 版本列表
        list_frame = tk.Frame(dlg, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=18, pady=(0, 10))

        canvas = tk.Canvas(list_frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        dlg.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        for vi, v in enumerate(versions_sorted):
            is_current = v["id"] == cur_id
            is_stable = v["id"] == stable_id
            vnum = v.get("version_number", "?")

            # 版本卡片背景
            card_colors = {
                (True, True): WARN,   # 当前且稳定
                (True, False): ACCENT,  # 当前非稳定
                (False, True): ACCENT_2,  # 稳定非当前
                (False, False): BG_INPUT,  # 普通
            }
            card_bg = card_colors.get((is_current, is_stable), BG_INPUT)
            card_fg = BG if (is_current or is_stable) else FG

            card = tk.Frame(scroll_frame, bg=card_bg, bd=1, relief="solid",
                           highlightbackground=BORDER, highlightthickness=0)
            card.pack(fill="x", pady=(0, 8), padx=0)

            # 版本头部
            header_row = tk.Frame(card, bg=card_bg)
            header_row.pack(fill="x", padx=12, pady=(8, 4))

            status_badges = []
            if is_current:
                status_badges.append("当前")
            if is_stable:
                status_badges.append("稳定")
            badge_text = " · ".join(status_badges) if status_badges else ""
            tk.Label(header_row, text=f"v{vnum}  {badge_text}", bg=card_bg, fg=card_fg,
                     font=(FONT, 13, "bold")).pack(side="left")
            tk.Label(header_row, text=v.get("created_at", "")[:16].replace("T", " "),
                     bg=card_bg, fg=card_fg if is_current or is_stable else FG_DIM,
                     font=(FONT, 9)).pack(side="right")

            # 变更说明
            changelog = v.get("changelog", "")
            if changelog:
                tk.Label(card, text=changelog, bg=card_bg, fg=card_fg if is_current or is_stable else FG_DIM,
                         font=(FONT, 10), anchor="w", justify="left", wraplength=700,
                         ).pack(fill="x", padx=12, pady=(0, 2))

            # 内容预览
            content_preview = v.get("content", "")[:200].replace("\n", " ")
            if len(v.get("content", "")) > 200:
                content_preview += "..."
            tk.Label(card, text=content_preview, bg=card_bg, fg=card_fg if is_current or is_stable else FG_DIM,
                     font=(FONT, 10), anchor="w", justify="left", wraplength=700,
                     ).pack(fill="x", padx=12, pady=(0, 4))

            # 操作按钮
            btn_row = tk.Frame(card, bg=card_bg)
            btn_row.pack(fill="x", padx=12, pady=(0, 8))

            if not is_current:
                self._btn(btn_row, "恢复此版本",
                          lambda vid=v["id"]: self._do_restore_version(snippet, vid, dlg),
                          ACCENT, BG, 10, False).pack(side="left", padx=(0, 6))
            if not is_stable:
                self._btn(btn_row, "标记稳定版",
                          lambda vid=v["id"]: self._do_mark_stable(snippet, vid, dlg),
                          ACCENT_2, BG, 10, False).pack(side="left", padx=(0, 6))
            if version_evidence_action(snippet, v["id"]) == "view_evidence":
                self._btn(btn_row, "查看验证证据",
                          lambda vid=v["id"]: self._show_version_evidence(snippet, vid),
                          BTN_SECONDARY, BTN_SECONDARY_FG, 10, False).pack(side="left", padx=(0, 6))
            if not is_current:
                self._btn(btn_row, "查看内容",
                          lambda vid=v["id"]: self._view_version_content(snippet, vid),
                          BTN_SECONDARY, BTN_SECONDARY_FG, 10).pack(side="left")

        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _do_restore_version(self, snippet, version_id, parent_dlg=None):
        if not messagebox.askyesno("恢复版本", "将从该版本创建新版（当前版不会丢失）。确定恢复？", parent=self.win):
            return
        self._restore_version(snippet, version_id)
        self._save_data()
        self._filter()
        if parent_dlg and parent_dlg.winfo_exists():
            parent_dlg.destroy()
            self._show_version_history(snippet)

    def _do_mark_stable(self, snippet, version_id, parent_dlg=None):
        self._mark_stable(snippet, version_id)
        self._save_data()
        self._filter()
        if parent_dlg and parent_dlg.winfo_exists():
            parent_dlg.destroy()
            self._show_version_history(snippet)

    def _show_version_evidence(self, snippet, version_id):
        """Open a detached, read-only view of one validated version's evidence."""
        try:
            evidence = get_version_evidence(self.data, snippet["id"], version_id)
        except VerificationEvidenceError as exc:
            messagebox.showerror("验证证据", str(exc), parent=self.win)
            return

        version = evidence["version"]
        baseline = evidence["baseline_version"]
        candidate = evidence["candidate"]
        repair_case = evidence["repair_case"]
        verification = evidence["verification"]
        analysis = evidence["analysis"] or {}
        source_counts = evidence["source_counts"]
        missing_fields = evidence["missing_provenance_fields"]

        dlg = tk.Toplevel(self.win)
        dlg.title(f"验证证据 · {snippet.get('title', '未命名提示词')} · v{version.get('version_number', '?')}")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("920x700")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"920x700+{(sw-920)//2}+{(sh-700)//2}")

        tk.Label(
            dlg,
            text="只读验证证据：该窗口不修改提示词、版本或验证记录。",
            bg=BG,
            fg=FG_DIM,
            font=(FONT, 10),
        ).pack(anchor="w", padx=18, pady=(16, 8))

        summary = tk.Frame(dlg, bg=BG_PANEL)
        summary.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(
            summary,
            text=(
                f"版本链：v{baseline.get('version_number', '?')} → v{version.get('version_number', '?')}\n"
                f"验证结论：{verification.get('overall_conclusion', '未记录')} · "
                f"验证状态：{verification.get('status', '未记录')}\n"
                f"候选修改原因：{'；'.join(candidate.get('change_reasons', [])) or '未记录'}\n"
                f"模型诊断：{analysis.get('diagnosis') or '历史记录未保存诊断摘要'}"
            ),
            bg=BG_PANEL,
            fg=FG,
            font=(FONT, 10),
            anchor="w",
            justify="left",
            wraplength=860,
        ).pack(fill="x", padx=12, pady=10)

        provenance = tk.Frame(dlg, bg=BG)
        provenance.pack(fill="x", padx=18, pady=(0, 8))
        source_text = " · ".join(
            f"{name} {count}" for name, count in source_counts.items() if count
        ) or "历史记录未记录样本来源"
        tk.Label(provenance, text=f"样本来源：{source_text}", bg=BG, fg=FG, font=(FONT, 10)).pack(anchor="w")
        if missing_fields:
            tk.Label(
                provenance,
                text="历史记录未记录样本来源/上下文快照：" + "、".join(missing_fields),
                bg=BG,
                fg=WARN,
                font=(FONT, 10),
                anchor="w",
                justify="left",
                wraplength=860,
            ).pack(anchor="w", pady=(3, 0))

        body = tk.Frame(dlg, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        text = tk.Text(
            body,
            bg=BG_INPUT,
            fg=FG,
            font=(FONT, 10),
            wrap="word",
            padx=10,
            pady=10,
            relief="flat",
            bd=0,
            highlightthickness=0,
            selectbackground=ACCENT,
            selectforeground=BG,
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        lines = [
            f"修复案例：{repair_case.get('id', '未记录')}",
            f"任务目标：{repair_case.get('task_goal') or '未记录'}",
            f"验证摘要：{verification.get('summary_note') or verification.get('output_note') or '未记录'}",
            "",
        ]
        for index, run in enumerate(verification.get("pairwise_runs") or [], start=1):
            lines.extend([
                f"===== 样本 {index} · {run.get('id', '未编号')} =====",
                f"来源：{run.get('source_type', '未记录')} · {run.get('source_label') or '无标签'}",
                f"用户确认：{'是' if run.get('user_confirmed') is True else '否/未记录'}",
                f"上下文范围：{run.get('context_scope') or '未记录'}",
                f"上下文：原始 {run.get('source_chars', '未记录')} 字符；实际发送 {run.get('context_chars', '未记录')} 字符；截断：{'是' if run.get('truncated') else '否'}",
                f"上下文哈希：{run.get('context_hash') or '未记录'}",
                f"上下文标签：{run.get('context_label') or '未记录'}",
                "[任务输入]",
                run.get('user_input') or "（空）",
                "[上下文快照]",
                run.get('context_text') or "（历史记录未保存）",
                "[基线输出]",
                run.get('baseline_output') or "（未记录）",
                f"耗时：{run.get('baseline_latency_ms', '未记录')} ms",
                "[候选输出]",
                run.get('candidate_output') or "（未记录）",
                f"耗时：{run.get('candidate_latency_ms', '未记录')} ms",
                f"人工裁决：{run.get('verdict') or '未记录'}",
                f"备注：{run.get('note') or '无'}",
                "",
            ])
        if not verification.get("pairwise_runs"):
            lines.append("该版本没有成对验证样本记录。")
        text.insert("1.0", "\n".join(lines))
        text.config(state="disabled")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _view_version_content(self, snippet, version_id):
        content = self._get_version_content(snippet, version_id)
        dlg = tk.Toplevel(self.win)
        dlg.title(f"查看版本内容 · {snippet['title']}")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("700x500")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"700x500+{(sw-700)//2}+{(sh-500)//2}")

        tf = tk.Frame(dlg, bg=BG)
        tf.pack(fill="both", expand=True, padx=18, pady=(16, 10))
        txt = tk.Text(tf, bg=BG_INPUT, fg=FG, font=(FONT, 12), wrap="word",
                      padx=10, pady=10, relief="flat", bd=0, highlightthickness=0,
                      selectbackground=ACCENT, selectforeground=BG)
        txt.insert("1.0", content)
        txt.config(state="disabled")
        scr = ttk.Scrollbar(tf, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scr.set)
        txt.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=18, pady=(0, 16))
        self._btn(bf, "复制内容", lambda: copy_to_clipboard(content), ACCENT, BG, 12, False).pack(side="right")
        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _show_variant_dialog(self, source_snippet):
        """创建变体对话框。产品规则3：scenario必填。"""
        dlg = tk.Toplevel(self.win)
        dlg.title(f"创建变体 · 基于「{source_snippet['title']}」")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("500x340")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"500x340+{(sw-500)//2}+{(sh-340)//2}")

        source_ver = snippet_version_label(source_snippet)
        tk.Label(dlg, text=f"基于：{source_snippet['title']} ({source_ver})", bg=BG, fg=FG_DIM,
                 font=(FONT, 10)).pack(anchor="w", padx=18, pady=(16, 4))

        tk.Label(dlg, text="变体名称", bg=BG, fg=FG, font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(8, 2))
        title_e = tk.Entry(dlg, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 13), bd=0, highlightthickness=0,
                           selectbackground=ACCENT, selectforeground=BG)
        title_e.pack(fill="x", padx=18, ipady=7)
        title_e.insert(0, f"{source_snippet['title']}（新变体）")

        tk.Label(dlg, text="使用场景 *", bg=BG, fg=FG, font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(10, 2))
        tk.Label(dlg, text="这个变体用在什么场景？", bg=BG, fg=FG_DIM,
                 font=(FONT, 9)).pack(anchor="w", padx=18, pady=(0, 2))
        scenario_e = tk.Entry(dlg, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 12), bd=0, highlightthickness=0,
                               selectbackground=ACCENT, selectforeground=BG)
        scenario_e.pack(fill="x", padx=18, ipady=6)
        scenario_e.focus_set()

        error_lbl = tk.Label(dlg, text="", bg=BG, fg=DANGER, font=(FONT, 10))
        error_lbl.pack(anchor="w", padx=18, pady=(6, 0))

        def do_create():
            title = title_e.get().strip()
            scenario = scenario_e.get().strip()
            if not title:
                error_lbl.config(text="名称不能为空")
                return
            if not scenario:
                error_lbl.config(text="使用场景不能为空")
                return
            new_s = self._create_variant(source_snippet, title, scenario)
            if new_s:
                self._save_data()
                self._render_categories()
                self._update_tags()
                self._filter()
                dlg.destroy()
            else:
                error_lbl.config(text="创建失败")

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=18, pady=(16, 16), side="bottom")
        self._btn(bf, "创建变体", do_create, ACCENT, BG, 12, False).pack(side="right", padx=4)
        self._btn(bf, "取消", dlg.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right", padx=4)

        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.bind("<Return>", lambda e: do_create())

    def _show_variant_info(self, snippet):
        """在操作栏显示变体信息。"""
        # 来源信息
        source = self._get_source_info(snippet)
        if source:
            src_title = source["title"]
            src_ver = source["version_label"]
            tk.Label(self.action_frame, text=f"← 来源于：{src_title}{src_ver}", bg=BG, fg=ACCENT_2,
                     font=(FONT, 10, "italic")).pack(side="left", padx=(10, 0))

        # 派生变体数量
        variants = self._get_variants(snippet)
        if variants:
            tk.Label(self.action_frame, text=f"→ {len(variants)} 个变体", bg=BG, fg=ACCENT_2,
                     font=(FONT, 10)).pack(side="left", padx=(10, 0))

    def _show_run_dialog(self, snippet):
        """使用评价对话框。"""
        dlg = tk.Toplevel(self.win)
        dlg.title(f"使用评价 · {snippet['title']}")
        apply_window_icon(dlg)
        dlg.configure(bg=BG)
        dlg.geometry("400x280")
        sw = dlg.winfo_screenwidth()
        sh = dlg.winfo_screenheight()
        dlg.geometry(f"400x280+{(sw-400)//2}+{(sh-280)//2}")

        tk.Label(dlg, text=f"给「{snippet['title']}」本次使用打分", bg=BG, fg=FG,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=18, pady=(16, 8))

        # 星级评分
        rating_var = tk.IntVar(value=0)
        stars_frame = tk.Frame(dlg, bg=BG)
        stars_frame.pack(fill="x", padx=18, pady=(0, 10))
        star_labels = []
        for i in range(1, 6):
            lbl = tk.Label(stars_frame, text="☆", bg=BG, fg=FG_DIM,
                          font=(FONT, 20), cursor="hand2")
            lbl.pack(side="left", padx=(0, 8))
            star_labels.append(lbl)

        def update_stars(val=None):
            for i, lbl in enumerate(star_labels):
                if i < rating_var.get():
                    lbl.config(text="★", fg=WARN)
                else:
                    lbl.config(text="☆", fg=FG_DIM)

        def set_rating(val):
            rating_var.set(val)
            update_stars()

        for i, lbl in enumerate(star_labels):
            lbl.bind("<Button-1>", lambda e, v=i+1: set_rating(v))
        update_stars()

        tk.Label(dlg, text="备注（可选）", bg=BG, fg=FG_DIM, font=(FONT, 10)).pack(anchor="w", padx=18, pady=(0, 2))
        note_e = tk.Entry(dlg, bg=BG_INPUT, fg=FG, insertbackground=FG, relief="flat", font=(FONT, 11), bd=0, highlightthickness=0,
                          selectbackground=ACCENT, selectforeground=BG)
        note_e.pack(fill="x", padx=18, ipady=5)

        def do_save():
            if rating_var.get() == 0:
                messagebox.showwarning("未评分", "请先给个评分", parent=dlg)
                return
            self._add_run(snippet["id"], snippet.get("current_version_id", ""),
                         rating_var.get(), note_e.get().strip())
            self._save_data()
            dlg.destroy()

        bf = tk.Frame(dlg, bg=BG)
        bf.pack(fill="x", padx=18, pady=(16, 16), side="bottom")
        self._btn(bf, "保存评价", do_save, ACCENT, BG, 12, False).pack(side="right", padx=4)
        self._btn(bf, "跳过", dlg.destroy, BTN_SECONDARY, BTN_SECONDARY_FG, 12).pack(side="right", padx=4)

        dlg.bind("<Escape>", lambda e: dlg.destroy())

    def _quit_app(self):
        """彻底退出应用。"""
        if hasattr(self, "hotkey_manager") and self.hotkey_manager:
            try:
                self.hotkey_manager.stop()
            except Exception:
                pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        if self.root:
            try:
                self.root.quit()
                self.root.destroy()
            except Exception:
                pass


# ── 入口 ──────────────────────────────────────────────────
class Win32HotkeyManager:
    """Windows RegisterHotKey listener with configurable key combination."""
    HOTKEY_ID = 101

    def __init__(self, callback, hotkey=HOTKEY):
        self.callback = callback
        self.hotkey = hotkey
        self.thread = None
        self.running = False
        self.registered = False

    @staticmethod
    def _key_parts(hotkey):
        parts = [part.strip().lower() for part in str(hotkey).split("+") if part.strip()]
        modifiers = {"ctrl": 0x0002, "control": 0x0002, "shift": 0x0004,
                     "alt": 0x0001, "win": 0x0008, "windows": 0x0008}
        key_map = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B,
                   **{chr(code + 0x30): 0x30 + code for code in range(10)},
                   **{chr(code + 0x41): 0x41 + code for code in range(26)}}
        if not parts or parts[-1] in modifiers:
            raise ValueError("热键必须包含一个主键")
        main_key = parts[-1]
        if main_key.startswith("f") and main_key[1:].isdigit():
            number = int(main_key[1:])
            if 1 <= number <= 12:
                key_map[main_key] = 0x70 + number - 1
        if main_key not in key_map:
            raise ValueError(f"暂不支持热键主键：{main_key}")
        flags = 0x4000
        for part in parts[:-1]:
            if part not in modifiers:
                raise ValueError(f"暂不支持热键修饰键：{part}")
            flags |= modifiers[part]
        if flags == 0x4000:
            raise ValueError("热键至少需要一个修饰键")
        return flags, key_map[main_key]

    def start(self):
        import ctypes
        user32 = ctypes.windll.user32
        modifiers, virtual_key = self._key_parts(self.hotkey)
        res = user32.RegisterHotKey(0, self.HOTKEY_ID, modifiers, virtual_key)
        if not res:
            err = ctypes.get_last_error()
            print(f"[Win32Hotkey] 注册热键失败，错误码: {err}，可能已被其他软件独占。", flush=True)
            self.registered = False
            return False
        self.registered = True
        print(f"[Win32Hotkey] 全局热键 {self.hotkey} 注册成功", flush=True)

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        import ctypes
        user32 = ctypes.windll.user32
        # 发送空消息解除 GetMessage 阻塞
        if self.thread and self.thread.ident:
            user32.PostThreadMessageW(self.thread.ident, 0x0000, 0, 0)

    def _loop(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        HOTKEY_ID = self.HOTKEY_ID
        msg = wintypes.MSG()
        try:
            while self.running:
                r = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if r <= 0 or not self.running:
                    break
                if msg.message == 0x0312:  # WM_HOTKEY
                    if msg.wParam == HOTKEY_ID:
                        self.callback()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(0, HOTKEY_ID)
            self.registered = False
            print("[Win32Hotkey] 热键已注销", flush=True)


def main():
    # Demo 模式跳过 kill，避免影响用户真实运行中的 PromptBox 实例
    if os.environ.get("PROMPTBOX_DEMO") != "1":
        kill_existing_instances()

    app = PromptBox()
    app.data = load_prompt_data()
    app.snippets = app.data["snippets"]
    app.root = tk.Tk()
    app.root.withdraw()
    # Windows：设置 AppUserModelID，让任务栏使用 PromptBox 图标而不是 python 默认
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PromptBox.App.1")
        except Exception:
            pass
    apply_window_icon(app.root)
    app.win = None

    q = queue.Queue()

    def on_hotkey():
        q.put(1)

    hotkey = app.data.get("preferences", {}).get("quick_copy_hotkey", HOTKEY)
    # 尝试在 Windows 下使用原生的 Win32 RegisterHotKey
    use_win32 = False
    if sys.platform == "win32":
        try:
            hotkey_mgr = Win32HotkeyManager(on_hotkey, hotkey)
            if hotkey_mgr.start():
                app.hotkey_manager = hotkey_mgr
                use_win32 = True
            else:
                # RegisterHotKey 失败（被其他软件独占）：fallback 到 keyboard 驱动
                app.hotkey_manager = None
        except Exception as e:
            print(f"[PromptBox] 启动 Win32 热键管理器失败: {e}，将回退到 keyboard 驱动", flush=True)
            app.hotkey_manager = None

    if not use_win32:
        try:
            keyboard.add_hotkey(hotkey, on_hotkey)
            print(f"[PromptBox] 已启动 (使用 keyboard 驱动)，按 {hotkey} 唤起", flush=True)
        except Exception as exc:
            print(f"[PromptBox] 热键 {hotkey} 注册失败：{exc}；恢复默认热键 {HOTKEY}", flush=True)
            keyboard.add_hotkey(HOTKEY, on_hotkey)
            hotkey = HOTKEY

    def poll():
        try:
            while True:
                q.get_nowait()
                app.root.after(0, app.toggle_palette)
        except queue.Empty:
            pass
        app.root.after(50, poll)

    app.root.after(50, poll)

    # Demo 模式：启动 1 秒后自动打开窗口，方便截图
    if os.environ.get("PROMPTBOX_DEMO") == "1":
        app.root.after(1000, app.toggle_palette)

    def on_closing():
        """彻底退出应用。"""
        # 先关闭 GUI 窗口防止事件循环中的 pending 事件操作已销毁控件
        if app.palette_win and app.palette_win.winfo_exists():
            try:
                app._close_palette()
            except Exception:
                pass
        if app.win and app.win.winfo_exists():
            try:
                app.win.destroy()
                app.win = None
            except Exception:
                pass
        if hasattr(app, "hotkey_manager") and app.hotkey_manager:
            try:
                app.hotkey_manager.stop()
            except Exception:
                pass
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            app.root.quit()
        except Exception:
            pass
        try:
            app.root.destroy()
        except Exception:
            pass

    app.root.protocol("WM_DELETE_WINDOW", on_closing)
    app.root.mainloop()


if __name__ == "__main__":
    main()
