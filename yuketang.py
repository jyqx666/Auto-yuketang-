#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雨课堂（慕课 / 学堂在线类课程）自动挂机看视频。

原理：用 Playwright 打开一个真实的 Edge / Chrome 窗口，微信扫码登录后，
脚本按章节顺序打开还没完成的视频页面，静音并按设定倍速真实播放，
播完自动进入下一个。观看进度由雨课堂自己的播放器上报，脚本不伪造任何数据，
也不处理作业、考试、讨论。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    from playwright.async_api import BrowserContext, Page, Response, async_playwright
    from playwright.async_api import Error as PlaywrightError
except ImportError:
    print("缺少依赖 playwright，请先运行 install.bat（或 pip install -r requirements.txt）")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "base_url": "https://www.yuketang.cn",
    "speed": 2.0,
    "mute": True,
    "browser": "msedge",
    "headless": False,
    "courses": [],
    "classroom_ids": [],
    "max_passes": 2,
    "chapter_wait_seconds": 30,
    "manual_wait_seconds": 300,
    "login_wait_seconds": 300,
    "course_page": "{base_url}/v2/web/studentLog/{classroom_id}",
    "video_page": "{base_url}/v2/web/xcloud/video-student/{classroom_id}/{leaf_id}",
}

VIDEO = 0
LEAF_TYPES = {0: "视频", 3: "图文", 4: "讨论", 5: "考试", 6: "作业"}

# 视频多久没有前进就认为卡住了（秒），以及卡住后最多刷新几次
STALL_SECONDS = 60
MAX_RELOADS = 3

log = logging.getLogger("yuketang")


# ---------------------------------------------------------------- 基础工具

def setup_logging(debug: bool) -> None:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    # 日志文件总是记录完整信息，出问题时方便排查
    logfile = logging.FileHandler(ROOT / "yuketang.log", encoding="utf-8")
    logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(console)
    log.addHandler(logfile)
    log.setLevel(logging.DEBUG)


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        # utf-8-sig：兼容用 Windows 记事本保存时带的 BOM
        with path.open(encoding="utf-8-sig") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    else:
        log.warning("没找到配置文件 %s，使用默认配置", path)
    cfg["base_url"] = str(cfg["base_url"]).rstrip("/")
    return cfg


def clamp_speed(speed: float) -> float:
    # 只用播放器自带的倍速档位范围，超出的倍速通常不计入进度
    if not 0.5 <= speed <= 2.0:
        clamped = min(max(speed, 0.5), 2.0)
        log.warning("倍速 %s 超出播放器支持的范围 0.5~2，已改为 %s", speed, clamped)
        return clamped
    return speed


def keep_awake(on: bool) -> None:
    """挂机期间阻止 Windows 自动睡眠（屏幕仍可以自动关闭）。"""
    if sys.platform != "win32":
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    flags = es_continuous | (es_system_required if on else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


async def ainput(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


async def wait_for(cond, timeout: float, interval: float = 0.5) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(interval)
    return bool(cond())


def fmt_time(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def split_ids(text: str) -> list[str]:
    return [x for x in text.replace("，", ",").replace(" ", ",").split(",") if x]


# ---------------------------------------------------------------- 解析接口数据

def find_key(obj, key: str, depth: int = 0):
    """在嵌套的 JSON 里找第一个名为 key 的字段，找不到返回 None。"""
    if depth > 10:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_key(child, key, depth + 1)
        if found is not None:
            return found
    return None


@dataclass
class Course:
    classroom_id: str
    name: str
    teacher: str = ""


@dataclass
class Leaf:
    id: str
    name: str
    leaf_type: int
    chapter: str

    @property
    def type_name(self) -> str:
        return LEAF_TYPES.get(self.leaf_type, f"类型{self.leaf_type}")

    @property
    def title(self) -> str:
        return f"{self.chapter} / {self.name}" if self.chapter else self.name


def parse_courses(data) -> list[Course]:
    """从课程列表接口里提取所有带 classroom_id 的班级。"""
    courses: list[Course] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if "classroom_id" in node:
            cid = str(node["classroom_id"])
            if cid in seen:
                return
            seen.add(cid)
            course = node.get("course") if isinstance(node.get("course"), dict) else {}
            course_name = course.get("name") or node.get("course_name") or node.get("name") or cid
            class_name = node.get("name") if node.get("name") not in (None, "", course_name) else ""
            teacher = node.get("teacher")
            teacher = teacher.get("name", "") if isinstance(teacher, dict) else str(teacher or "")
            name = f"{course_name}（{class_name}）" if class_name else str(course_name)
            courses.append(Course(cid, name, teacher))
            return
        for value in node.values():
            walk(value)

    walk(data.get("data", data) if isinstance(data, dict) else data)
    return courses


def parse_leaves(chapter_data) -> list[Leaf]:
    """把章节目录（course_chapter）展开成按顺序排列的学习单元列表。"""
    leaves: list[Leaf] = []
    seen: set[str] = set()

    def walk(node, path: list[str]) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, path)
            return
        if not isinstance(node, dict):
            return
        if "leaf_type" in node and "id" in node and not isinstance(node.get("leaf_list"), list):
            leaf_id = str(node["id"])
            if leaf_id not in seen:
                seen.add(leaf_id)
                try:
                    leaf_type = int(node["leaf_type"])
                except (TypeError, ValueError):
                    leaf_type = -1
                leaves.append(Leaf(leaf_id, str(node.get("name") or leaf_id), leaf_type, " / ".join(path)))
            return
        name = node.get("name") or node.get("title")
        has_children = any(isinstance(v, (list, dict)) for v in node.values())
        sub_path = path + [str(name)] if name and has_children else path
        for value in node.values():
            if isinstance(value, (list, dict)):
                walk(value, sub_path)

    walk(find_key(chapter_data, "course_chapter"), [])
    return leaves


def parse_schedules(data) -> dict[str, float] | None:
    """解析学习进度接口里的 leaf_schedules：{单元ID: 进度(1 表示完成)}。"""
    raw = find_key(data, "leaf_schedules")
    if not isinstance(raw, dict):
        return None
    result = {}
    for leaf_id, value in raw.items():
        if isinstance(value, dict):
            value = next((value[k] for k in ("schedule", "rate", "progress", "completed") if k in value), 0)
        try:
            result[str(leaf_id)] = float(value)
        except (TypeError, ValueError):
            pass
    return result


def parse_watch_completed(data, leaf_id: str) -> bool | None:
    """解析视频页的观看进度接口，返回该视频是否已完成；没有相关数据返回 None。"""
    info = find_key(data, str(leaf_id))
    if isinstance(info, dict) and "completed" in info:
        try:
            return int(info["completed"] or 0) == 1
        except (TypeError, ValueError):
            return None
    return None


def is_done(progress: float | None) -> bool:
    return progress is not None and progress >= 0.999


# ---------------------------------------------------------------- 抓取网页自己发出的接口数据

class Capture:
    """
    监听浏览器收到的接口响应。章节目录、学习进度这些接口的参数（课程签名等）
    由雨课堂网页自己算好并请求，脚本只读取响应，这样不用关心接口参数细节。
    """

    def __init__(self, base_url: str):
        self.host = urlparse(base_url).hostname or ""
        self.chapter = None
        self.schedule: dict[str, float] | None = None
        self.watch_progress: list = []

    def reset(self) -> None:
        self.chapter = None
        self.schedule = None
        self.watch_progress = []

    def _is_ours(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        return host == self.host or host.endswith(".yuketang.cn")

    async def on_response(self, resp: Response) -> None:
        try:
            if not self._is_ours(resp.url) or "json" not in (resp.headers.get("content-type") or ""):
                return
            data = await resp.json()
        except Exception:
            return
        try:
            if find_key(data, "course_chapter") is not None:
                self.chapter = data
                log.debug("抓到章节目录：%s", resp.url)
            schedule = parse_schedules(data)
            if schedule is not None:
                self.schedule = schedule
                log.debug("抓到学习进度：%s", resp.url)
            if "get_video_watch_progress" in resp.url:
                self.watch_progress.append(data)
        except Exception as e:  # 解析失败不影响挂机
            log.debug("解析 %s 失败：%s", resp.url, e)

    def video_completed(self, leaf_id: str) -> bool | None:
        for data in reversed(self.watch_progress):
            result = parse_watch_completed(data, leaf_id)
            if result is not None:
                return result
        return None


# ---------------------------------------------------------------- 浏览器

# 让页面始终以为自己在前台：窗口最小化或被遮挡时，页面不会因此暂停视频
KEEP_VISIBLE_JS = """
(() => {
  try {
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    document.hasFocus = () => true;
    const stop = e => { if (e.target === window || e.target === document) e.stopImmediatePropagation(); };
    document.addEventListener('visibilitychange', stop, true);
    window.addEventListener('visibilitychange', stop, true);
    window.addEventListener('blur', stop, true);
  } catch (e) {}
})();
"""

# 找到页面上的 <video>，保持静音、倍速和播放状态，并返回当前进度
VIDEO_TICK_JS = """
(args) => {
  const videos = Array.from(document.querySelectorAll('video'));
  const v = videos.find(x => x.duration > 0) || videos[0];
  if (!v) return null;
  if (args.restart) v.currentTime = 0;
  if (args.mute) v.muted = true;
  if (Math.abs(v.playbackRate - args.speed) > 0.01) v.playbackRate = args.speed;
  if (args.play && v.paused && !v.ended) {
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
  }
  const dur = isFinite(v.duration) ? v.duration : 0;
  return { cur: v.currentTime, dur: dur, paused: v.paused, ended: v.ended, ready: v.readyState };
}
"""


async def launch_browser(pw, cfg: dict) -> BrowserContext:
    user_dir = ROOT / "browser_data"
    wanted = cfg["browser"] if cfg["browser"] not in ("", "chromium", None) else None
    # 优先用系统自带的 Edge / Chrome：Playwright 自带的 Chromium 不支持 H.264，播不了大部分课程视频
    channels = [wanted] + [c for c in ("msedge", "chrome") if c != wanted] + [None]
    last_error = None
    for channel in dict.fromkeys(channels):
        try:
            ctx = await pw.chromium.launch_persistent_context(
                str(user_dir),
                channel=channel,
                headless=bool(cfg["headless"]),
                no_viewport=True,
                args=["--autoplay-policy=no-user-gesture-required", "--start-maximized"],
            )
        except PlaywrightError as e:
            last_error = e
            continue
        if channel != wanted:
            log.warning("没能启动 %s，改用 %s", wanted or "chromium", channel or "Playwright 自带的 Chromium")
        if channel is None:
            log.warning("自带的 Chromium 可能播不了课程视频，建议安装 Edge 或 Chrome")
        return ctx
    raise SystemExit(f"浏览器启动失败：{last_error}")


async def video_tick(page: Page, args: dict) -> dict | None:
    for frame in page.frames:
        try:
            state = await frame.evaluate(VIDEO_TICK_JS, args)
        except PlaywrightError:
            continue
        if state:
            return state
    return None


# ---------------------------------------------------------------- 登录与选课

async def api_get_json(ctx: BrowserContext, url: str, headers: dict | None = None):
    try:
        resp = await ctx.request.get(url, headers=headers or {}, timeout=20000)
        if not resp.ok:
            return None
        return await resp.json()
    except Exception:
        return None


async def api_headers(ctx: BrowserContext, cfg: dict) -> dict:
    cookies = await ctx.cookies(cfg["base_url"])
    headers = {"xtbz": "ykt", "x-requested-with": "XMLHttpRequest", "referer": cfg["base_url"] + "/v2/web/index"}
    csrftoken = next((c["value"] for c in cookies if c["name"] == "csrftoken"), "")
    if csrftoken:
        headers["x-csrftoken"] = csrftoken
    return headers


def api_ok(data) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("success") is False:
        return False
    return data.get("errcode", 0) in (0, "0")


async def fetch_courses(ctx: BrowserContext, cfg: dict) -> list[Course] | None:
    """已登录时返回课程列表（可能为空），未登录或接口不可用返回 None。"""
    headers = await api_headers(ctx, cfg)
    data = await api_get_json(ctx, f"{cfg['base_url']}/v2/api/web/courses/list?identity=2", headers)
    if api_ok(data):
        courses = parse_courses(data)
        cookies = await ctx.cookies(cfg["base_url"])
        if courses or any(c["name"] == "sessionid" for c in cookies):
            return courses
    data = await api_get_json(ctx, f"{cfg['base_url']}/v2/api/web/userinfo", headers)
    if api_ok(data) and data.get("data"):
        return []  # 已登录，但课程列表接口没拿到数据
    return None


async def login(ctx: BrowserContext, page: Page, cfg: dict) -> list[Course]:
    await page.goto(cfg["base_url"] + "/v2/web/index", wait_until="domcontentloaded")
    courses = await fetch_courses(ctx, cfg)
    if courses is not None:
        log.info("已登录（沿用上次保存的登录状态）")
        return courses

    shot = ROOT / "login_qrcode.png"
    if cfg["headless"]:
        log.info("无头模式：登录页截图保存在 %s，请打开它用微信扫码", shot)
    else:
        log.info("请在弹出的浏览器窗口里用微信扫码登录雨课堂（页面没有二维码就点一下“登录”）")
    deadline = time.monotonic() + float(cfg["login_wait_seconds"])
    while time.monotonic() < deadline:
        if cfg["headless"]:
            try:
                await page.screenshot(path=str(shot))
            except PlaywrightError:
                pass
        await asyncio.sleep(3)
        courses = await fetch_courses(ctx, cfg)
        if courses is not None:
            log.info("登录成功，登录状态已保存，下次运行不用再扫码")
            return courses
    raise SystemExit("等待扫码登录超时，请重新运行")


async def choose_courses(courses: list[Course], cfg: dict, select_all: bool) -> list[Course]:
    known = {c.classroom_id: c for c in courses}
    if cfg["classroom_ids"]:
        return [known.get(str(i)) or Course(str(i), f"班级 {i}") for i in cfg["classroom_ids"]]

    if cfg["courses"]:
        selected = [c for c in courses if any(str(k) in c.name for k in cfg["courses"])]
        if selected:
            return selected
        log.warning("配置里的课程关键词 %s 没有匹配到课程，改为手动选择", cfg["courses"])

    if not courses:
        text = await ainput(
            "没有获取到课程列表。请输入课程的班级 ID（在浏览器里打开课程，网址 studentLog/ 后面的数字），"
            "多个用逗号分隔：")
        return [Course(i, f"班级 {i}") for i in split_ids(text)]

    print()
    for i, c in enumerate(courses, 1):
        teacher = f"  [{c.teacher}]" if c.teacher else ""
        print(f"  {i:>2}. {c.name}{teacher}  (班级ID {c.classroom_id})")
    print()
    if select_all:
        return courses
    while True:
        text = await ainput("请输入要刷的课程序号（多个用逗号或空格分隔，直接回车 = 全部）：")
        if not text:
            return courses
        try:
            numbers = [int(x) for x in split_ids(text)]
        except ValueError:
            numbers = []
        if numbers and all(1 <= n <= len(courses) for n in numbers):
            return [courses[n - 1] for n in dict.fromkeys(numbers)]
        print("输入有误，请重新输入，例如：1,3")


# ---------------------------------------------------------------- 挂机主流程

async def load_course_page(page: Page, cap: Capture, course: Course, cfg: dict, interactive: bool) -> bool:
    """打开课程页，等网页自己加载章节目录和学习进度。"""
    cap.reset()
    url = cfg["course_page"].format(base_url=cfg["base_url"], classroom_id=course.classroom_id)
    await page.goto(url, wait_until="domcontentloaded")
    if await wait_for(lambda: cap.chapter is not None, float(cfg["chapter_wait_seconds"])):
        await wait_for(lambda: cap.schedule is not None, 8)
        return True

    # 有的课程要先点到“学习内容”之类的标签页才会加载目录
    for text in ("学习内容", "课程内容", "章节", "目录"):
        try:
            await page.get_by_text(text, exact=True).first.click(timeout=2000)
        except PlaywrightError:
            continue
        if await wait_for(lambda: cap.chapter is not None, 8):
            await wait_for(lambda: cap.schedule is not None, 8)
            return True

    if not interactive:
        return False
    log.warning("没有自动找到这门课的章节目录。请在浏览器里手动点开这门课、进入能看到视频列表的页面，"
                "脚本检测到后会自动继续（最多等 %s 秒，超时跳过这门课）", cfg["manual_wait_seconds"])
    if await wait_for(lambda: cap.chapter is not None, float(cfg["manual_wait_seconds"])):
        await wait_for(lambda: cap.schedule is not None, 8)
        return True
    return False


async def watch_video(page: Page, cap: Capture, course: Course, leaf: Leaf, cfg: dict, restart: bool) -> bool:
    url = cfg["video_page"].format(base_url=cfg["base_url"], classroom_id=course.classroom_id, leaf_id=leaf.id)
    speed, mute = cfg["speed"], bool(cfg["mute"])
    cap.watch_progress = []
    await page.goto(url, wait_until="domcontentloaded")

    state = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = await video_tick(page, {"speed": speed, "mute": mute, "play": False, "restart": False})
        if state and state["dur"] > 0:
            break
        await asyncio.sleep(1)
    if state is None:
        log.warning("    页面上没找到视频（可能还没开放，或需要手动操作），跳过")
        return False
    if not restart and cap.video_completed(leaf.id):
        log.info("    这个视频已经看完了，跳过")
        return True

    started = time.monotonic()
    last_cur, last_move, last_log = -1.0, started, 0.0
    reloads = 0
    first = True
    while True:
        state = await video_tick(page, {"speed": speed, "mute": mute, "play": True, "restart": restart and first})
        first = False
        now = time.monotonic()
        if state:
            cur, dur = state["cur"], state["dur"]
            if state["ended"] or (dur > 0 and cur >= dur - 0.5):
                log.info("    播放完成 %s", fmt_time(dur))
                await asyncio.sleep(8)  # 给播放器留时间把最后的进度上报出去
                return True
            if abs(cur - last_cur) > 0.1:
                last_cur, last_move = cur, now
            if now - last_log >= 30:
                last_log = now
                pct = f"{cur / dur * 100:5.1f}%" if dur else "  -  "
                log.info("    %s / %s  %s  %gx", fmt_time(cur), fmt_time(dur), pct, speed)
            limit = (dur / speed) * 2 + 600 if dur else 3 * 3600
            if now - started > limit:
                log.warning("    播放时间远超视频时长，放弃这个视频")
                return False

        if now - last_move > STALL_SECONDS:
            reloads += 1
            if reloads > MAX_RELOADS:
                log.warning("    视频一直卡住，跳过（如果所有视频都这样，多半是浏览器播不了这种视频格式，"
                            "请确认用的是 Edge 或 Chrome）")
                return False
            log.warning("    视频 %s 秒没动了，刷新页面重试（第 %d 次）", STALL_SECONDS, reloads)
            await page.reload(wait_until="domcontentloaded")
            last_cur, last_move = -1.0, time.monotonic()
        await asyncio.sleep(3)


async def run_course(page: Page, cap: Capture, course: Course, cfg: dict) -> None:
    log.info("========== %s ==========", course.name)
    if not await load_course_page(page, cap, course, cfg, interactive=not cfg["headless"]):
        log.warning("拿不到章节目录，跳过这门课（如果它不是慕课类课程，这是正常的）")
        return

    leaves = parse_leaves(cap.chapter)
    videos = [leaf for leaf in leaves if leaf.leaf_type == VIDEO]
    schedule = cap.schedule
    log.info("共 %d 个学习单元，其中视频 %d 个%s", len(leaves), len(videos),
             "" if schedule is not None else "（没拿到学习进度，将逐个打开检查）")

    watched: set[str] = set()
    failed: set[str] = set()

    def video_done(leaf: Leaf) -> bool:
        if schedule is not None:
            return is_done(schedule.get(leaf.id))
        return leaf.id in watched

    for round_no in range(1, int(cfg["max_passes"]) + 1):
        todo = [v for v in videos if not video_done(v)]
        if not todo:
            break
        restart = round_no > 1
        if restart:
            log.info("第 %d 轮：还有 %d 个视频进度没满，从头重新播放", round_no, len(todo))
        else:
            log.info("待播放视频 %d 个", len(todo))
        for i, leaf in enumerate(todo, 1):
            log.info("  [%d/%d] %s", i, len(todo), leaf.title)
            if await watch_video(page, cap, course, leaf, cfg, restart):
                watched.add(leaf.id)
                failed.discard(leaf.id)
            else:
                failed.add(leaf.id)

        if schedule is None:
            break  # 拿不到进度就没法核对，相信播放结束事件
        # 重新打开课程页刷新学习进度，核对哪些视频还没算完成
        if await load_course_page(page, cap, course, cfg, interactive=False) and cap.schedule is not None:
            schedule = cap.schedule
        else:
            log.warning("刷新学习进度失败，无法核对完成情况")
            break

    done = sum(1 for v in videos if video_done(v))
    log.info("视频完成情况：%d / %d", done, len(videos))
    for leaf in videos:
        if not video_done(leaf):
            log.info("  未完成：%s", leaf.title)

    others = [leaf for leaf in leaves if leaf.leaf_type != VIDEO]
    if schedule is not None:
        others = [leaf for leaf in others if not is_done(schedule.get(leaf.id))]
    if others:
        counts: dict[str, int] = {}
        for leaf in others:
            counts[leaf.type_name] = counts.get(leaf.type_name, 0) + 1
        summary = "、".join(f"{name} {n} 个" for name, n in counts.items())
        log.info("还有 %s%s需要你自己完成（脚本只负责视频）：", summary, "未完成，" if schedule is not None else "")
        for leaf in others[:30]:
            log.info("  [%s] %s", leaf.type_name, leaf.title)
        if len(others) > 30:
            log.info("  ……共 %d 项，完整列表见 yuketang.log", len(others))
            for leaf in others[30:]:
                log.debug("  [%s] %s", leaf.type_name, leaf.title)


async def main() -> None:
    parser = argparse.ArgumentParser(description="雨课堂慕课视频自动挂机")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径")
    parser.add_argument("--speed", type=float, help="播放倍速，覆盖配置文件")
    parser.add_argument("--all", action="store_true", help="刷全部课程，不询问")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    args = parser.parse_args()

    setup_logging(args.debug)
    cfg = load_config(Path(args.config))
    if args.speed:
        cfg["speed"] = args.speed
    cfg["speed"] = clamp_speed(float(cfg["speed"]))

    keep_awake(True)
    try:
        async with async_playwright() as pw:
            ctx = await launch_browser(pw, cfg)
            cap = Capture(cfg["base_url"])
            ctx.on("response", cap.on_response)
            await ctx.add_init_script(KEEP_VISIBLE_JS)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                courses = await login(ctx, page, cfg)
                selected = await choose_courses(courses, cfg, args.all)
                if not selected:
                    log.info("没有选择任何课程")
                    return
                log.info("开始挂机，倍速 %gx。浏览器窗口可以最小化，但不要关闭。", cfg["speed"])
                for course in selected:
                    await run_course(page, cap, course, cfg)
                log.info("全部课程处理完毕")
            finally:
                await ctx.close()
    finally:
        keep_awake(False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已手动停止。下次运行会自动跳过已完成的视频。")
    except PlaywrightError as e:
        if "closed" in str(e).lower():
            print("\n浏览器窗口被关闭，脚本已退出。")
        else:
            raise
