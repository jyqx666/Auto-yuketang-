#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雨课堂（慕课 / 学堂在线类课程）自动挂机看视频。

原理：用 Playwright 打开一个真实的 Edge / Chrome 窗口，微信扫码登录后，
脚本按章节顺序打开还没完成的视频页面，静音并按设定倍速真实播放，
播完自动进入下一个。“继续观看”之类的提示会自动点掉；视频里弹出题目时，
脚本会暂停并提醒你自己作答。观看进度由雨课堂自己的播放器上报，脚本不伪造任何数据，
也不替你答题，不处理作业、考试、讨论。
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
    from playwright.async_api import BrowserContext, Frame, Page, Response, async_playwright
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
    "quiz_wait_minutes": 30,
    "course_page": "{base_url}/v2/web/studentLog/{classroom_id}",
    # 按顺序尝试，哪个打开后有视频就一直用哪个（雨课堂新版学习空间用第一个）
    "video_page": [
        "{base_url}/ai-workspace/lms-graph/{classroom_id}/video/{leaf_id}",
        "{base_url}/v2/web/xcloud/video-student/{classroom_id}/{leaf_id}",
    ],
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
    if isinstance(cfg["video_page"], str):
        cfg["video_page"] = [cfg["video_page"]]
    cfg["video_page"] = list(cfg["video_page"])
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

# 记录你手动点击过的元素（只记真实的鼠标点击，不记脚本自己的点击），写进日志，方便排查倍速菜单之类的页面结构
CLICK_RECORDER_JS = """
(() => {
  window.__yktClicks = [];
  document.addEventListener('click', e => {
    if (!e.isTrusted || window.__yktBusy) return;
    try {
      const el = e.target;
      const path = [];
      for (let n = el, i = 0; n && n.nodeType === 1 && i < 4; n = n.parentElement, i++) {
        const cls = typeof n.className === 'string' ? n.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
        path.push(n.tagName.toLowerCase() + (cls ? '.' + cls : ''));
      }
      const attrs = [...el.attributes].filter(a => a.name !== 'class' && a.name !== 'style')
        .slice(0, 5).map(a => `${a.name}=${a.value.slice(0, 20)}`).join(' ');
      const text = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 30);
      window.__yktClicks.push(`${path.join(' < ')} [${attrs}] "${text}"`);
      if (window.__yktClicks.length > 20) window.__yktClicks.shift();
    } catch (err) {}
  }, true);
})();
"""

# 静音保护：播放器每次自己设置静音或音量时，先记下它想要的状态（用来判断播放器自己是不是静音），
# 然后马上把视频静音、音量调为 0，所以播放器按自己记住的音量恢复声音也不会出声。
# 脚本自己静音走 window.__yktSilence，不算作播放器的设置。计数上限防止和播放器无限来回。
MUTE_GUARD_JS = """
(() => {
  const proto = HTMLMediaElement.prototype;
  const md = Object.getOwnPropertyDescriptor(proto, 'muted');
  const vd = Object.getOwnPropertyDescriptor(proto, 'volume');
  if (!md || !md.set || !vd || !vd.set) return;
  let own = false, fixes = 0;
  const silence = (el, fromPage) => {
    if (md.get.call(el) && vd.get.call(el) === 0) return;
    if (fromPage && ++fixes > 5000) return;
    own = true;
    try { md.set.call(el, true); vd.set.call(el, 0); } finally { own = false; }
  };
  window.__yktSilence = el => silence(el, false);
  Object.defineProperty(proto, 'muted', {
    configurable: true, enumerable: md.enumerable, get: md.get,
    set(value) {
      md.set.call(this, value);
      if (own) return;
      this.__yktPageMuted = !!value;
      silence(this, true);
    },
  });
  Object.defineProperty(proto, 'volume', {
    configurable: true, enumerable: vd.enumerable, get: vd.get,
    set(value) {
      vd.set.call(this, value);
      if (own) return;
      this.__yktPageVolume = Number(value);
      silence(this, true);
    },
  });
  for (const type of ['loadedmetadata', 'play', 'playing', 'volumechange']) {
    document.addEventListener(type, e => {
      if (e.target instanceof HTMLMediaElement) silence(e.target, true);
    }, true);
  }
})();
"""

# 给主视频（显示面积最大的那个）打上标记，用鼠标操作前先把鼠标移到它上面，让自动隐藏的控制栏显示出来
MARK_VIDEO_JS = """
() => {
  document.querySelectorAll('[data-ykt-video]').forEach(e => e.removeAttribute('data-ykt-video'));
  const area = x => { const r = x.getBoundingClientRect(); return r.width * r.height; };
  const v = [...document.querySelectorAll('video')].sort((a, b) => area(b) - area(a))[0];
  if (v) v.setAttribute('data-ykt-video', '');
  return !!v;
}
"""

# 判断播放器自己是不是静音（看它最后一次对主视频的静音、音量设置），并给它的静音按钮打上标记。
# 雨课堂播放器的静音按钮是 <xt-volumebutton> 里的 <xt-icon>，其余是常见网页播放器的写法。
MUTE_JS = """
() => {
  const area = x => { const r = x.getBoundingClientRect(); return r.width * r.height; };
  const v = [...document.querySelectorAll('video')].sort((a, b) => area(b) - area(a))[0];
  if (!v) return null;
  const pm = v.__yktPageMuted, pv = v.__yktPageVolume;
  const intent = (pm === true || pv === 0) ? 'muted' : (pm === false || pv > 0) ? 'unmuted' : 'unknown';
  const selectors = [
    'xt-volumebutton xt-icon', 'xt-volumebutton', '.xgplayer-volume .xgplayer-icon', '.vjs-mute-control',
    '[aria-label*="静音"]', '[aria-label*="mute" i]', '[title*="静音"]', '[title*="mute" i]',
    '[class*="volume" i][class*="icon" i]', '[class*="volume" i][class*="btn" i]', '[class*="volume" i][class*="button" i]',
  ];
  let button = null;
  for (const sel of selectors) {
    button = document.querySelector(sel);
    if (button) break;
  }
  document.querySelectorAll('[data-ykt-mute]').forEach(e => e.removeAttribute('data-ykt-mute'));
  if (button) button.setAttribute('data-ykt-mute', '');
  const cls = button && typeof button.className === 'string' ? button.className.trim().split(/\\s+/)[0] : '';
  return { intent, button: button ? `${button.tagName.toLowerCase()}${cls ? '.' + cls : ''}` : null };
}
"""

# 静音页面上所有的音视频；找出主视频（正在显示的面积最大的那个，页面上可能还有隐藏的预加载视频），
# act 为 true 时再对主视频执行倍速、从头播放、继续播放；返回主视频的状态
VIDEO_TICK_JS = """
(args) => {
  const media = [...document.querySelectorAll('video, audio')];
  if (args.mute) {
    for (const m of media) {
      if (window.__yktSilence) {
        window.__yktSilence(m);
      } else {
        if (!m.muted) m.muted = true;
        if (m.volume !== 0) m.volume = 0;
      }
    }
  }
  const area = x => { const r = x.getBoundingClientRect(); return r.width * r.height; };
  const videos = media.filter(m => m.tagName === 'VIDEO')
    .sort((a, b) => (area(b) - area(a)) || ((b.duration > 0) - (a.duration > 0)));
  const v = videos[0];
  if (!v) return null;
  const rate = v.playbackRate;
  if (args.act) {
    if (args.reset && v.currentTime > 0) v.currentTime = 0;
    if (args.rate && Math.abs(v.playbackRate - args.rate) > 0.01) v.playbackRate = args.rate;
    if (args.play && v.paused && !v.ended) {
      const p = v.play();
      if (p && p.catch) p.catch(() => {});
    }
  }
  const dur = isFinite(v.duration) ? v.duration : 0;
  return { cur: v.currentTime, dur, paused: v.paused, ended: v.ended, ready: v.readyState, rate,
           area: area(v), count: videos.length, silent: v.muted || v.volume === 0 };
}
"""


# 处理视频页上的弹窗：
#   - 视频里弹出的题目（可见的“提交/确定”按钮 + 同一容器里 2~12 个选项）只报告，不作答
#   - “继续观看 / 我知道了”这类单按钮提示直接点掉；从头重播时优先点“从头观看”
POPUP_JS = """
(args) => {
  const vis = e => !!(e && (e.offsetParent || e.getClientRects().length));
  const txt = e => (e.textContent || '').trim().replace(/\\s+/g, ' ');
  const buttons = [...document.querySelectorAll('button, [role=button], a.btn, .btn')].filter(vis);
  const submit = buttons.find(e => /^(提交|提交答案|确定)$/.test(txt(e)));
  if (submit) {
    let root = submit;
    for (let i = 0; i < 10 && root.parentElement; i++) {
      root = root.parentElement;
      const options = [...root.querySelectorAll(
        'li, label, [class*="option" i], [class*="choice" i], [class*="answer" i]')]
        .filter(e => vis(e) && txt(e) && txt(e).length <= 120 && !e.contains(submit));
      if (options.length >= 2 && options.length <= 12) return { quiz: true };
    }
  }
  const find = names => {
    for (const name of names) {
      const el = buttons.find(e => txt(e) === name);
      if (el) return el;
    }
    return null;
  };
  const el = (args.restart && find(['从头观看', '从头播放', '重新观看', '重新播放']))
    || find(['继续观看', '继续学习', '继续播放', '我知道了', '知道了']);
  if (el) {
    el.click();
    return { clicked: txt(el) };
  }
  return null;
}
"""


# 在播放器自带的倍速菜单里选倍速。通过菜单选，播放器自己记录的倍速和界面显示才会跟着变，
# 不会播一会儿又被改回 1 倍。倍速选项按以下特征识别（兼容雨课堂和常见的网页播放器）：
#   - 带倍速属性的元素：data-speed / keyt（雨课堂 xt-speedlist）、cname（西瓜播放器）、data-rate
#   - 文字是“2.0X”“2x”“2倍”“×2”这类的元素
#   - 倍速菜单（class 含 speed / rate）里文字是纯数字的元素
# 选不超过目标的最高档位；页面上没有倍速菜单返回 null。
# click 为 false 时只给选项和倍速按钮打上标记（交给 Playwright 用真实鼠标去点），为 true 时用脚本点击。
SPEED_JS = """
({ target, click }) => {
  const ATTRS = ['data-speed', 'keyt', 'cname', 'data-rate'];
  const BOX = 'xt-speedlist, xt-speedbutton, [class*="speed" i], [class*="rate" i], [class*="beisu" i]';
  const own = e => (e.textContent || '').trim().replace(/\\s+/g, '');
  const parse = e => {
    for (const a of ATTRS) {
      const raw = e.getAttribute(a);
      if (raw && /^\\d+(\\.\\d+)?$/.test(raw.trim())) return { v: parseFloat(raw), score: 4 };
    }
    if (e.children.length) return null;
    const t = own(e);
    let m = t.match(/^(\\d+(?:\\.\\d+)?)(?:x|×|倍|倍速)$/i) || t.match(/^[x×](\\d+(?:\\.\\d+)?)$/i);
    if (m) return { v: parseFloat(m[1]), score: 2 };
    m = t.match(/^(\\d+(?:\\.\\d+)?)$/);
    if (m && e.closest(BOX)) return { v: parseFloat(m[1]), score: 1 };
    return null;
  };
  const options = [];
  for (const e of document.querySelectorAll('li, [data-speed], [keyt], [cname], [data-rate], span, div, a, button, p')) {
    const r = parse(e);
    if (!r || !(r.v >= 0.25 && r.v <= 16)) continue;
    if (e.tagName === 'LI') r.score += 1;
    if (e.closest('ul, ol, xt-speedlist, [class*="list" i], [class*="menu" i]')) r.score += 1;
    options.push({ e, v: r.v, score: r.score });
  }
  if (!options.length) return null;
  const available = [...new Set(options.map(o => o.v))].sort((a, b) => a - b);
  const usable = available.filter(v => v <= target + 1e-6);
  if (!usable.length) return { picked: null, available };
  const value = usable[usable.length - 1];
  const best = options.filter(o => o.v === value).sort((a, b) => b.score - a.score)[0].e;
  const cls = typeof best.className === 'string' ? best.className.trim().split(/\\s+/)[0] : '';
  const result = { picked: value, available, clicked: `${best.tagName.toLowerCase()}${cls ? '.' + cls : ''} "${own(best)}"` };

  if (!click) {
    document.querySelectorAll('[data-ykt-speed-option], [data-ykt-speed-hover]').forEach(e => {
      e.removeAttribute('data-ykt-speed-option');
      e.removeAttribute('data-ykt-speed-hover');
    });
    best.setAttribute('data-ykt-speed-option', '');
    const button = best.closest('xt-speedbutton') || document.querySelector('xt-speedbutton')
      || best.parentElement.closest('[class*="speed" i], [class*="rate" i]');
    if (button) button.setAttribute('data-ykt-speed-hover', '');
    return result;
  }

  // 先把鼠标“移到”倍速按钮上展开菜单，再完整地模拟一次点击
  const hover = [document.querySelector('xt-speedbutton')];
  for (let n = best.parentElement, i = 0; n && i < 4; n = n.parentElement, i++) hover.push(n);
  for (const h of hover.filter(Boolean)) {
    for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
      h.dispatchEvent(new MouseEvent(type, { bubbles: type !== 'mouseenter' }));
    }
  }
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup']) {
    const Ctor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
    best.dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, button: 0 }));
  }
  best.click();
  return result;
}
"""

# 找不到倍速菜单时，把页面上和倍速有关的元素记进日志，方便排查
SPEED_DEBUG_JS = """
() => [...document.querySelectorAll('[class*="speed" i], [class*="rate" i], xt-speedbutton, xt-speedlist, [data-speed]')]
  .slice(0, 15)
  .map(e => `${e.tagName.toLowerCase()}.${String(e.className || '').slice(0, 60)} | ${(e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60)}`)
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
    """在所有框架里静音并找到主视频（显示面积最大的），只对主视频执行播放、倍速等操作。"""
    best, best_frame = None, None
    for frame in page.frames:
        try:
            state = await frame.evaluate(VIDEO_TICK_JS, {**args, "act": False})
        except PlaywrightError:
            continue
        if state and (best is None or (state["area"], state["dur"]) > (best["area"], best["dur"])):
            best, best_frame = state, frame
    if best is None:
        return None
    if args.get("play") or args.get("reset") or args.get("rate"):
        try:
            best = await best_frame.evaluate(VIDEO_TICK_JS, {**args, "act": True}) or best
        except PlaywrightError:
            pass
    return best


async def log_manual_clicks(page: Page) -> None:
    """把你在浏览器里手动点过的元素写进日志文件。"""
    for frame in page.frames:
        try:
            clicks = await frame.evaluate("() => { const c = window.__yktClicks || []; window.__yktClicks = []; return c; }")
        except PlaywrightError:
            continue
        for click in clicks or []:
            log.debug("    你手动点击了：%s", click)


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

async def click_tab(page: Page) -> bool:
    """点“学习内容”之类的标签页，章节目录一般要点开它才会加载。"""
    for text in ("学习内容", "课程内容", "章节", "目录"):
        for frame in page.frames:
            target = frame.get_by_text(text, exact=True)
            try:
                if await target.count() == 0:
                    continue
                await target.first.click(timeout=3000)
            except PlaywrightError:
                continue
            log.debug("点击了「%s」", text)
            return True
    return False


async def load_course_page(page: Page, cap: Capture, course: Course, cfg: dict, interactive: bool) -> bool:
    """打开课程页，等网页自己加载章节目录和学习进度。"""
    cap.reset()
    url = cfg["course_page"].format(base_url=cfg["base_url"], classroom_id=course.classroom_id)
    await page.goto(url, wait_until="domcontentloaded")

    # 课程页默认显示“学习日志”，目录要点到“学习内容”才加载；页面渲染慢时多试几次
    opened = time.monotonic()
    deadline = opened + float(cfg["chapter_wait_seconds"])
    clicked = False
    next_click = opened + 3
    while cap.chapter is None and time.monotonic() < deadline:
        await asyncio.sleep(1)
        if not clicked and time.monotonic() >= next_click:
            clicked = await click_tab(page)
            next_click = time.monotonic() + 5

    if cap.chapter is None and interactive:
        log.warning("没有自动找到这门课的章节目录。请在浏览器里手动点开这门课、进入能看到视频列表的页面，"
                    "脚本检测到后会自动继续（最多等 %s 秒，超时跳过这门课）", cfg["manual_wait_seconds"])
        await wait_for(lambda: cap.chapter is not None, float(cfg["manual_wait_seconds"]))
    if cap.chapter is None:
        return False
    await wait_for(lambda: cap.schedule is not None, 8)
    return True


def alert_user() -> None:
    if sys.platform == "win32":
        import winsound

        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    else:
        print("\a", end="", flush=True)


async def handle_popups(page: Page, restart: bool) -> dict:
    result: dict = {}
    for frame in page.frames:
        try:
            found = await frame.evaluate(POPUP_JS, {"restart": restart})
        except PlaywrightError:
            continue
        if found:
            result.update(found)
    return result


async def wait_video(page: Page, cfg: dict, timeout: float) -> dict | None:
    """等页面上的视频加载出来，页面上没有 <video> 返回 None。"""
    state = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await video_tick(page, {"rate": None, "mute": bool(cfg["mute"]), "play": False, "reset": False})
        if state and state["dur"] > 0:
            break
        await asyncio.sleep(1)
    return state


async def open_video(page: Page, cap: Capture, course: Course, leaf: Leaf, cfg: dict) -> bool:
    """按 video_page 里的网址格式依次尝试打开视频页，能用的格式挪到最前面，后面的视频直接用它。"""
    templates: list[str] = cfg["video_page"]
    for template in list(templates):
        url = template.format(base_url=cfg["base_url"], classroom_id=course.classroom_id, leaf_id=leaf.id)
        cap.watch_progress = []
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as e:
            log.debug("打开 %s 失败：%s", url, e)
            continue
        if await wait_video(page, cfg, 30) is not None:
            if template != templates[0]:
                templates.remove(template)
                templates.insert(0, template)
            return True
        log.debug("%s 上没有视频", url)
    return False


async def mouse_click(page: Page, frame: Frame, target: str, hover: str | None = None) -> bool:
    """
    像手动操作一样用鼠标点击 frame 里的 target（产生真实的鼠标事件）：先把鼠标移到视频上，
    让自动隐藏的控制栏显示出来，再悬停 hover（比如倍速按钮），点击 target，最后把鼠标移开。
    """
    try:
        await frame.evaluate("() => { window.__yktBusy = true; }")  # 脚本自己的点击不记成你的手动点击
        if await frame.evaluate(MARK_VIDEO_JS):
            await frame.locator("[data-ykt-video]").first.hover(timeout=2000, force=True)
            await asyncio.sleep(0.3)
        if hover and await frame.locator(hover).count():
            await frame.locator(hover).first.hover(timeout=3000, force=True)
        await frame.locator(target).first.click(timeout=3000, force=True)
        return True
    except PlaywrightError as e:
        log.debug("    用鼠标点击 %s 失败：%s", target, str(e).splitlines()[0])
        return False
    finally:
        try:
            await page.mouse.move(1, 1)  # 把鼠标移开，让菜单收起来
        except PlaywrightError:
            pass
        try:
            await frame.evaluate("() => { window.__yktBusy = false; }")
        except PlaywrightError:
            pass


class SpeedControl:
    """
    让视频按设定倍速播放：
      - 优先在播放器自带的倍速菜单里选。先用真实的鼠标操作（悬停倍速按钮、再点选项，和手动操作一样），
        不行再用脚本点击。雨课堂的播放器只有这样选，自己记录的倍速才会变，之后不会被改回去。
      - 同时直接设置 <video> 的倍速作为兜底。
      - 视频刚加载的一段时间里，播放器可能会把倍速改回去，所以每次检查都看实际倍速，
        没生效就一直重试，不会放弃。
    """

    MENU_INTERVAL = 6      # 倍速没生效时，隔多少秒重新在菜单里选一次
    SLOW_AFTER = 120       # 超过这么多秒还没生效，就提示一次，并放慢重试
    SLOW_INTERVAL = 30
    CONFIRM_AFTER = 10     # 实际倍速连续保持这么多秒才算生效

    def __init__(self, page: Page, wanted: float):
        self.page = page
        self.wanted = wanted
        self.target = wanted
        self.has_menu = False
        self.direct = False
        self.mismatch_since: float | None = None
        self.last_menu = 0.0
        self.ok_since: float | None = None
        self.confirmed = False
        self.warned = False

    async def _eval_frames(self, js: str, arg=None):
        for frame in self.page.frames:
            try:
                result = await frame.evaluate(js, arg)
            except PlaywrightError:
                continue
            if result:
                return result
        return None

    async def _select_in_menu(self) -> dict | None:
        self.last_menu = time.monotonic()
        for frame in self.page.frames:
            try:
                found = await frame.evaluate(SPEED_JS, {"target": self.wanted, "click": False})
            except PlaywrightError:
                continue
            if not found:
                continue
            if found.get("picked"):
                if await mouse_click(self.page, frame, "[data-ykt-speed-option]", hover="[data-ykt-speed-hover]"):
                    found["how"] = "鼠标"
                else:
                    try:
                        await frame.evaluate(SPEED_JS, {"target": self.wanted, "click": True})
                        found["how"] = "脚本"
                    except PlaywrightError:
                        pass
            return found
        return None

    async def start(self) -> None:
        result = await self._select_in_menu()
        self.has_menu = bool(result and result.get("picked"))
        if self.has_menu:
            self.target = result["picked"]
            if self.target < self.wanted:
                log.info("    播放器最高只有 %g 倍速（可选：%s），按 %g 倍播放",
                         self.target, "、".join(f"{v:g}" for v in result["available"]), self.target)
            log.debug("    在倍速菜单里点了 %s（%s）", result.get("clicked"), result.get("how", "未点击"))
            return
        self.direct = True
        if result:
            log.info("    播放器的倍速档位里没有 %g 倍（可选：%s），直接设置视频倍速",
                     self.wanted, "、".join(f"{v:g}" for v in result["available"]))
        else:
            log.info("    没找到播放器的倍速菜单，直接设置视频倍速为 %g 倍", self.wanted)
            log.debug("    页面上和倍速有关的元素：%s", await self._eval_frames(SPEED_DEBUG_JS))

    def tick_rate(self) -> float | None:
        """需要直接改 <video> 倍速时返回目标倍速，交给 VIDEO_TICK_JS 去设置。"""
        return self.target if self.direct else None

    async def check(self, actual: float) -> None:
        now = time.monotonic()
        if abs(actual - self.target) <= 0.01:
            if self.ok_since is None:
                self.ok_since = now
            if not self.confirmed and now - self.ok_since >= self.CONFIRM_AFTER:
                self.confirmed = True
                log.info("    倍速已生效：%g 倍", self.target)
            self.mismatch_since = None
            return
        self.ok_since = None
        if self.mismatch_since is None:
            self.mismatch_since = now
        stuck = now - self.mismatch_since
        log.debug("    实际倍速 %g，目标 %g（已持续 %.0f 秒）", actual, self.target, stuck)
        self.direct = True
        interval = self.MENU_INTERVAL if stuck < self.SLOW_AFTER else self.SLOW_INTERVAL
        if self.has_menu and now - self.last_menu >= interval:
            result = await self._select_in_menu()
            log.debug("    重新在倍速菜单里点了 %s（%s）", result and result.get("clicked"), result and result.get("how"))
        if stuck >= self.SLOW_AFTER and not self.warned:
            self.warned = True
            log.warning("    倍速 %d 秒内一直被播放器改回 %g 倍，脚本会继续尝试。如果在播放器里手动选 %g 倍也不行，"
                        "说明这门课限制了倍速", self.SLOW_AFTER, actual, self.wanted)
            log.debug("    页面上和倍速有关的元素：%s", await self._eval_frames(SPEED_DEBUG_JS))


class MuteControl:
    """
    像手动操作一样点一下播放器自己的静音按钮，让播放器自己也处于静音（界面上显示静音）。
    声音本身始终由脚本直接静音保证，这里只管播放器的状态。静音按钮是开关，所以：
      - 先看播放器自己是不是静音（它最后一次对视频的静音、音量设置），已经是就不点；
      - 点完核对一次，点反了就再点回来；点了看不出效果就不再点，免得反而把播放器的声音打开。
    """

    MAX_CLICKS = 4
    CHECK_INTERVAL = 30

    def __init__(self, page: Page):
        self.page = page
        self.clicks = 0
        self.stopped = False
        self.next_check = time.monotonic() + 3  # 等视频开始播放、播放器设好自己的音量再看

    async def _find(self) -> tuple[Frame | None, dict | None]:
        for frame in self.page.frames:
            try:
                state = await frame.evaluate(MUTE_JS)
            except PlaywrightError:
                continue
            if state:
                return frame, state
        return None, None

    async def ensure(self) -> None:
        self.next_check = time.monotonic() + self.CHECK_INTERVAL
        while not self.stopped and self.clicks < self.MAX_CLICKS:
            frame, state = await self._find()
            if not state or state["intent"] == "muted":
                return
            if not state["button"]:
                log.debug("    没找到播放器的静音按钮，只直接静音视频")
                self.stopped = True
                return
            before = state["intent"]
            if not await mouse_click(self.page, frame, "[data-ykt-mute]"):
                try:
                    await frame.evaluate("() => { const b = document.querySelector('[data-ykt-mute]'); if (b) b.click(); }")
                except PlaywrightError:
                    self.stopped = True
                    return
            self.clicks += 1
            await asyncio.sleep(1)
            _, state = await self._find()
            after = state["intent"] if state else None
            log.debug("    点了播放器的静音按钮 %s：%s → %s", state and state["button"], before, after)
            if after == "muted":
                log.info("    已点击播放器的静音按钮")
                return
            if after == before:
                self.stopped = True
                return
            # 状态变了但不是静音（播放器原本就是静音，被点成了有声音），回到循环再点一次


async def watch_video(page: Page, cap: Capture, course: Course, leaf: Leaf, cfg: dict, restart: bool) -> bool:
    if not await open_video(page, cap, course, leaf, cfg):
        log.warning("    页面上没找到视频（可能还没开放，或需要手动操作），跳过")
        return False
    if not restart and cap.video_completed(leaf.id):
        log.info("    这个视频已经看完了，跳过")
        return True

    speed, mute = cfg["speed"], bool(cfg["mute"])
    speed_ctl = SpeedControl(page, speed)
    await speed_ctl.start()
    mute_ctl = MuteControl(page) if mute else None
    quiz_wait = float(cfg["quiz_wait_minutes"]) * 60
    started = time.monotonic()
    # 第一条进度在开播几秒后再打印，那时倍速已经设好，显示的是实际倍速
    last_cur, last_move, last_log = -1.0, started, started - 25
    prev_cur, prev_tick = None, started
    quiz_since, last_remind, quiz_total = None, 0.0, 0.0
    reloads = 0
    first = True
    while True:
        await log_manual_clicks(page)
        popup = await handle_popups(page, restart)
        if popup.get("clicked"):
            log.info("    已自动点掉提示「%s」", popup["clicked"])
        quiz = bool(popup.get("quiz"))
        state = await video_tick(page, {"rate": speed_ctl.tick_rate(), "mute": mute, "play": not quiz,
                                        "reset": restart and first})
        first = False
        now = time.monotonic()

        # 视频里弹出了题目：脚本不替你答，暂停等你作答，等待的时间不算卡住
        if quiz and state and state["paused"]:
            if quiz_since is None:
                quiz_since = last_remind = now
                log.warning("    视频里弹出了题目，请到浏览器窗口里自己作答，答完脚本会自动继续")
                alert_user()
                try:
                    await page.bring_to_front()
                except PlaywrightError:
                    pass
            elif now - last_remind >= 300:
                last_remind = now
                log.warning("    还在等你作答视频里的题目……")
                alert_user()
            if quiz_wait > 0 and now - quiz_since > quiz_wait:
                log.warning("    %g 分钟没有作答，先跳过这个视频", float(cfg["quiz_wait_minutes"]))
                return False
            last_move = now
            await asyncio.sleep(3)
            continue
        if quiz_since is not None:
            quiz_total += now - quiz_since
            quiz_since = None
            log.info("    题目已作答，继续播放")

        if state:
            cur, dur = state["cur"], state["dur"]
            # 从头重播时，播放器可能自己跳回上次看到的位置，跳了就再拉回开头
            if (restart and prev_cur is not None and now - started < 120
                    and cur - prev_cur > (now - prev_tick) * speed + 5):
                log.info("    播放器跳到了上次看到的位置，拉回开头重新播放")
                state = await video_tick(page, {"rate": speed_ctl.tick_rate(), "mute": mute, "play": True,
                                                "reset": True}) or state
                cur = state["cur"]
            prev_cur, prev_tick = cur, now
            await speed_ctl.check(state["rate"])
            if mute_ctl and now >= mute_ctl.next_check and not state["paused"]:
                await mute_ctl.ensure()

            if state["ended"] or (dur > 0 and cur >= dur - 0.5):
                log.info("    播放完成 %s", fmt_time(dur))
                await asyncio.sleep(8)  # 给播放器留时间把最后的进度上报出去
                return True
            if abs(cur - last_cur) > 0.1:
                last_cur, last_move = cur, now
            if now - last_log >= 30:
                last_log = now
                pct = f"{cur / dur * 100:5.1f}%" if dur else "  -  "
                log.info("    %s / %s  %s  %gx  %s", fmt_time(cur), fmt_time(dur), pct, state["rate"],
                         "静音" if state["silent"] else "有声音")
            # 按 1 倍速估算上限，倍速没生效时也不会误判
            limit = dur / min(speed, 1.0) * 1.5 + 600 if dur else 3 * 3600
            if now - started - quiz_total > limit:
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
            if await wait_video(page, cfg, 30) is not None:
                speed_ctl = SpeedControl(page, speed)
                await speed_ctl.start()
                mute_ctl = MuteControl(page) if mute else None
            last_cur, last_move = -1.0, time.monotonic()
            prev_cur = None  # 刷新后播放器会续播到刚才的位置，不算跳转
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
            await ctx.add_init_script(CLICK_RECORDER_JS)
            if cfg["mute"]:
                await ctx.add_init_script(MUTE_GUARD_JS)
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
