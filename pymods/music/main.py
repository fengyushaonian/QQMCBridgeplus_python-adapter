# -*- coding: utf-8 -*-
"""点歌器子插件：搜索音乐并直接发送 mp3 语音条到群（QQMCBridgeplus 下游适配版）。

用法：点歌 <关键词>（如「点歌 晴天」）→ 网易云音乐搜索 → 取最相关一条 →
下载 mp3 到 media/ → 以语音条（file_type=3）发送，附歌名/歌手文字。

下游适配说明（相对上游 QQMCBridge-releasenew 的差异）：
  - 上游：返回 {"type":"voice","url": 公网URL}，URL 由根配置 media_public_base_url 拼接，
    未配置该字段即报「media_public_base_url 未配置，无法发送文件」。
  - 下游：**无需任何公网配置**。返回 {"type":"voice","url": 本地文件路径}，
    网关自动走「base64 直传本地文件 → 换取 file_info → 发送语音条」链路；
    媒体与 content 文字会由网关自动拆成两条消息（先语音后文字，与上游一致）。

网易云外链接口：https://music.163.com/song/media/outer/url?id=<id>.mp3
（该接口对可外链歌曲返回 CDN mp3，无版权歌曲返回 404，需带 Referer 下载。）
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import requests

FOLDER = Path(__file__).parent
MEDIA_DIR = FOLDER.parent.parent / "media"
LOG = logging.getLogger("music")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
REF = "https://music.163.com/"

help = "点歌 <关键词>：搜索并发送 mp3 歌曲（语音条）"


def _read_settings() -> dict:
    try:
        data = json.loads((FOLDER / "config.json").read_text(encoding="utf-8"))
        settings = data.get("settings", {}) if isinstance(data, dict) else {}
        return settings if isinstance(settings, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_data() -> dict:
    try:
        data = json.loads((FOLDER / "data.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_data(data: dict) -> None:
    (FOLDER / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _search_music(keyword: str, limit: int) -> list:
    """网易云搜索 → [{id, name, artist, album, dt}]，失败抛 RuntimeError。"""
    response = requests.post(
        "https://music.163.com/api/search/get/web",
        data={"s": keyword, "type": 1, "limit": limit, "offset": 0},
        headers={"User-Agent": UA, "Referer": REF,
                 "Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    data = response.json()
    if data.get("code") != 200 or not data.get("result", {}).get("songs"):
        raise RuntimeError(f"未搜到「{keyword}」相关歌曲")
    songs = []
    for song in data["result"]["songs"][:limit]:
        artists = song.get("ar") or song.get("artists") or []
        songs.append({
            "id": int(song.get("id") or 0),
            "name": str(song.get("name") or "未知歌曲"),
            "artist": " / ".join(str(a.get("name")) for a in artists if a.get("name")) or "未知歌手",
            "album": str(((song.get("al") or song.get("album") or {}).get("name")) or ""),
            "dt": int(song.get("dt") or song.get("duration") or 0),
        })
    return songs


def _download_mp3(song_id: int, target: Path, max_bytes: int, timeout: float) -> int:
    """网易云外链下载 mp3；失败/超限抛 RuntimeError。"""
    url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
    with requests.get(url, stream=True, allow_redirects=True, timeout=timeout,
                      headers={"User-Agent": UA, "Referer": REF}) as response:
        if response.status_code != 200:
            raise RuntimeError(f"下载失败：HTTP {response.status_code}（可能版权受限）")
        content_type = str(response.headers.get("Content-Type", ""))
        if "audio" not in content_type and "octet-stream" not in content_type:
            raise RuntimeError("接口未返回音频文件（可能版权受限）")
        size = 0
        with open(target, "wb") as handle:
            for chunk in response.iter_content(65536):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    handle.close()
                    target.unlink(missing_ok=True)
                    raise RuntimeError(f"文件超过大小限制（>{max_bytes // 1024 // 1024}MB）")
                handle.write(chunk)
    if size < 1024:  # 极小文件基本是错误页
        target.unlink(missing_ok=True)
        raise RuntimeError("文件异常（可能版权受限）")
    return size


def _fmt_dt(milliseconds) -> str:
    seconds = max(0, int(milliseconds or 0)) // 1000
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}:{secs:02d}"


def _safe_filename(name: str) -> str:
    """清理文件名里的非法字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    return name or "music"


async def handle_message(ctx):
    settings = _read_settings()
    if settings.get("enabled", True) is False:
        return None

    text = str(ctx.content or "").strip()
    prefix = str(settings.get("prefix") or "点歌").strip()
    matched = None
    for candidate in [prefix, "点一首", "music"]:
        if text == candidate:
            matched = candidate
            break
        if text.startswith(candidate + " "):
            matched = candidate
            break
    if not matched:
        return None

    keyword = text[len(matched):].strip()
    if not keyword:
        return f"**[点歌]** 用法：{prefix} <歌曲名/歌手>（如「{prefix} 晴天」）"

    # 防抖：同一人 30s 内不重复点歌
    cooldown = max(0.0, float(settings.get("cooldown_seconds") or 30))
    data = _load_data()
    now = time.time()
    last = float(data.get(ctx.sender_openid, 0) or 0)
    if cooldown > 0 and now - last < cooldown:
        left = int(cooldown - (now - last) + 0.999)
        return f"**[点歌]** 请等待 {left} 秒后再点歌"
    data[ctx.sender_openid] = now
    _save_data(data)

    try:
        limit = max(1, min(10, int(settings.get("limit") or 5)))
        songs = await asyncio.to_thread(_search_music, keyword, limit)
        # QQ 富媒体音频时长上限约 5 分钟（40093013），过滤超长候选，选限制内最匹配的
        max_dur_ms = int(settings.get("max_duration_seconds") or 280) * 1000
        ok_songs = [s for s in songs if s["dt"] <= max_dur_ms]
        if not ok_songs:
            return (f"**[点歌]** 没搜到时长 {int(max_dur_ms / 1000)} 秒内的结果"
                    f"（QQ 音频上限约 5 分钟），换个关键词试试")
        song = ok_songs[0]
        title = f"{song['name']} - {song['artist']}"

        max_bytes = int(settings.get("max_size_mb") or 20) * 1024 * 1024
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        target = MEDIA_DIR / f"music_{song['id']}_{int(time.time())}.mp3"
        try:
            size = await asyncio.to_thread(
                _download_mp3, song["id"], target, max_bytes,
                float(settings.get("download_timeout") or 60))
        except Exception as download_error:
            target.unlink(missing_ok=True)
            LOG.warning("点歌下载失败 %s：%s", title, download_error)
            return (f"**[点歌]** 《{song['name']}》下载失败：{download_error}\n"
                    f"试试其它关键词，或稍后再试")

        # 下游适配：直接返回「本地文件路径 + 说明文字」。
        # 网关识别 type=voice 后自动 base64 直传本地文件（无需公网 URL / media_public_base_url），
        # 并把 content 文字作为第二条消息发送（先语音后文字）。
        size_mb = size / 1024 / 1024
        album_note = f" ｜ 专辑《{song['album']}》" if song.get("album") else ""
        content = (f"🎵 **{song['name']}** - {song['artist']}{album_note}\n"
                   f"时长 {_fmt_dt(song['dt'])} ｜ 大小 {size_mb:.1f}MB\n"
                   f"发送「{prefix} <关键词>」搜索更多歌曲")
        return {"type": "voice", "url": str(target), "content": content}
    except Exception as error:
        LOG.warning("点歌解析失败 [%s]：%s", keyword, error)
        return f"**[点歌]** {error}"
