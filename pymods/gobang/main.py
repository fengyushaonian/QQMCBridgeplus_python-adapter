# -*- coding: utf-8 -*-
"""Python 子插件：群内 15×15 五子棋（图片版），支持人机对战（多难度 AI）。

演示 pymods 子插件的状态管理与「发图」能力：
- 每个群维护一局对局（GAMES）；
- 落子后把棋盘渲染成图片卡片发到群（复用网关渲染器与中文字体）；
- 渲染器不可用时自动回退为 ASCII 棋盘文本；
- 内置 AI 对手，分四档难度：简单 / 普通 / 困难 / 大师。

指令速览（完整版见「五子棋 帮助」）：
  五子棋            双人开局（黑先）
  五子棋 人机        人机开局，默认难度「普通」
  五子棋 人机 困难   人机开局并指定难度（简单/普通/困难/大师）
  落子 A3           在 A 列第 3 行落子（列 A~O，行 1~15）
  落子 1 3          也可以用数字 列 行（空格分隔）
  五子棋 认输       当前轮到的一方认输，对方获胜
  五子棋 结束       结束当前对局
  五子棋 帮助       查看完整玩法与指令说明
  五子棋 难度       查看难度档位说明

发图接口见 pymods/README.md（ctx.gateway.send_card / ctx.IMAGE_SENT）。
"""

import re
import random

help = ("五子棋：开局「五子棋」/ 人机「五子棋 人机 [简单|普通|困难|大师]」/ 落子「落子 A3」"
        "或「落子 1 3」/ 认输「五子棋 认输」/ 结束「五子棋 结束」/ 玩法「五子棋 帮助」")

SIZE = 15
EMPTY, BLACK, WHITE = 0, 1, 2

# ---------------------------------------------------------------------------
# 难度分级：别名映射 -> 内部档位键
# ---------------------------------------------------------------------------
DIFFICULTY_KEYS = ["简单", "普通", "困难", "大师"]
_DIFFICULTY_ALIASES = {
    "简单": ["简单", "易", "菜鸟", "新手", "初级", "easy", "简单难度"],
    "普通": ["普通", "一般", "中级", "normal", "默认", "标准"],
    "困难": ["困难", "难", "高级", "hard", "进阶"],
    "大师": ["大师", "最强", "宗师", "无敌", "master", "地狱", "骨灰"],
}
_ALIAS_TO_KEY = {}
for _key, _aliases in _DIFFICULTY_ALIASES.items():
    _ALIAS_TO_KEY[_key.lower()] = _key
    for _a in _aliases:
        _ALIAS_TO_KEY[_a.lower()] = _key

DEFAULT_DIFFICULTY = "普通"

# 每群一局：group_openid -> 对局状态字典
GAMES: dict = {}

# 卡片「内部」片段：不含 <!DOCTYPE>/<html>/<body>/<head>，字体与玻璃外壳由
# ctx.gateway.glass_wrap 注入。.card 改为半透明以透出随机背景与玻璃效果。
_CARD_TEMPLATE = """<style>
  * { margin:0; padding:0; box-sizing:border-box; font-family:'GenJyuuGothic','Microsoft YaHei','PingFang SC',sans-serif; }
  .card { background:rgba(255,255,255,0.6); border:1px solid rgba(255,255,255,0.6); border-radius:18px; width:100%; padding:20px; text-align:center; box-shadow:0 2px 10px rgba(0,0,0,0.1); }
  .title { font-size:26px; color:#c85a12; font-weight:bold; }
  .subtitle { font-size:16px; color:#e8881f; margin-top:2px; }
  .turn { font-size:20px; color:#3a2a18; margin:8px 0 14px; }
  .board { display:grid; grid-template-columns:repeat(15, 34px); grid-template-rows:repeat(15, 34px);
    width:510px; margin:0 auto; background:rgba(236,210,176,0.85); border:3px solid #c98a3c; border-radius:8px; }
  .cell { position:relative; border:1px solid #e0bd8e; }
  .stone { position:absolute; left:4px; top:4px; width:26px; height:26px; border-radius:50%; }
  .black { background:radial-gradient(circle at 35% 30%, #555, #000); }
  .white { background:radial-gradient(circle at 35% 30%, #fff, #cfcfcf); box-shadow:0 0 2px #999; }
  .lastmark { position:absolute; right:2px; top:2px; width:9px; height:9px; border-radius:50%; background:#e23b3b; }
</style>
<div class="card">
  <div class="title">⚫⚪ 五子棋</div>
  <div class="subtitle">__SUBTITLE__</div>
  <div class="turn">__TURN__</div>
  <div class="board">__CELLS__</div>
</div>"""

_HELP_TEXT = (
    "♟️ 五子棋玩法与指令\n"
    "━━━━━━━━━━━━━━\n"
    "【开局 / 模式】\n"
    "· 五子棋            双人开局（黑先，对方落子即执白）\n"
    "· 五子棋 人机       人机开局（你执黑先行，AI 执白）\n"
    "· 五子棋 人机 难度   人机开局并指定难度，如：五子棋 人机 大师\n"
    "  难度档位：简单 / 普通 / 困难 / 大师（默认「普通」）\n\n"
    "【落子】\n"
    "· 落子 A3           字母列(A~O) + 数字行(1~15)\n"
    "· 落子 1 3          也可数字 列 行（空格分隔）\n"
    "  人机模式下落子后 AI 会自动应手。\n\n"
    "【结束对局】\n"
    "· 五子棋 认输       当前轮到的一方认输，对方获胜\n"
    "· 五子棋 结束       结束当前对局\n\n"
    "【其他】\n"
    "· 五子棋 帮助       显示本说明\n"
    "· 五子棋 难度       查看 AI 难度档位说明\n\n"
    "规则：两人（或你与 AI）轮流落子，率先五子连珠（横/竖/斜）者胜。"
)

_DIFFICULTY_HELP = (
    "🤖 AI 难度档位\n"
    "━━━━━━━━━━━━━━\n"
    "· 简单：只会「能赢就赢、该堵就堵」，其余随机落子，新手友好。\n"
    "· 普通：单层贪心，综合进攻与防守选择最优落子。\n"
    "· 困难：带一步前瞻，会规避你即将形成的杀招，攻守更稳。\n"
    "· 大师：极小化极大搜索（α-β 剪枝），棋力最强，慎战！\n\n"
    "开局示例：五子棋 人机 大师"
)


# ---------------------------------------------------------------------------
# 棋盘基础工具
# ---------------------------------------------------------------------------
def _new_board():
    return [[EMPTY] * SIZE for _ in range(SIZE)]


def _in_bounds(r, c):
    return 0 <= r < SIZE and 0 <= c < SIZE


def _is_win_at(board, r, c, color):
    """以 (r,c) 为终点检查四个方向是否五连。"""
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        cnt = 1
        for s in (1, -1):
            nr, nc = r + dr * s, c + dc * s
            while _in_bounds(nr, nc) and board[nr][nc] == color:
                cnt += 1
                nr += dr * s
                nc += dc * s
        if cnt >= 5:
            return True
    return False


def _board_full(board):
    for r in range(SIZE):
        for c in range(SIZE):
            if board[r][c] == EMPTY:
                return False
    return True


def _parse_move(text: str):
    """解析「落子 ...」坐标，返回 (row, col) 下标或 None。"""
    body = text.strip()[len("落子"):].strip()
    if not body:
        return None
    m = re.match(r"^([A-Oa-o])\s*(\d{1,2})$", body)
    if m:
        col = ord(m.group(1).upper()) - ord("A")
        row = int(m.group(2)) - 1
        return (row, col)
    m = re.match(r"^(\d{1,2})\s*[ ,，\-]\s*(\d{1,2})$", body)
    if m:
        col = int(m.group(1)) - 1
        row = int(m.group(2)) - 1
        return (row, col)
    return None


# ---------------------------------------------------------------------------
# AI 评估与搜索
# ---------------------------------------------------------------------------
# 棋子连珠模式评分（针对「放置后」的某条线，'O'=己方 'X'=对方/边界 '_'=空）
_LIVE_FOUR = re.compile(r"_OOOO_")
_FOUR_GAP = re.compile(r"OO_OO")
_FOUR_SIMPLE = re.compile(r"(?<!O)OOOO(?!O)")
_LIVE_THREE = re.compile(r"_OOO_")
_THREE_SIMPLE = re.compile(r"(?<!O)OOO(?!O)")
_GAP_THREE = re.compile(r"_OO_O_|_O_OO_")
_LIVE_TWO = re.compile(r"_OO_")
_GAP_TWO = re.compile(r"_O_O_")
_TWO_SIMPLE = re.compile(r"(?<!O)OO(?!O)")
_ONE = re.compile(r"_O_")

_WIN_SCORE = 10_000_000
_INF = 10 ** 9


def _score_line(s: str) -> int:
    """返回某条线上己方（'O'）最佳形状的得分。"""
    if "OOOOO" in s:
        return _WIN_SCORE
    score = 0
    if _LIVE_FOUR.search(s):
        score = max(score, 10000)
    if _FOUR_GAP.search(s):
        score = max(score, 5000)
    for m in _FOUR_SIMPLE.finditer(s):
        i, j = m.start(), m.end()
        if (i - 1 >= 0 and s[i - 1] == "_") or (j < len(s) and s[j] == "_"):
            score = max(score, 5000)
    if _LIVE_THREE.search(s):
        score = max(score, 2000)
    for m in _THREE_SIMPLE.finditer(s):
        i, j = m.start(), m.end()
        lo = (i - 1 >= 0 and s[i - 1] == "_")
        ro = (j < len(s) and s[j] == "_")
        if (lo or ro) and not (lo and ro):
            score = max(score, 500)
    if _GAP_THREE.search(s):
        score = max(score, 500)
    if _LIVE_TWO.search(s):
        score = max(score, 100)
    if _GAP_TWO.search(s):
        score = max(score, 20)
    for m in _TWO_SIMPLE.finditer(s):
        i, j = m.start(), m.end()
        lo = (i - 1 >= 0 and s[i - 1] == "_")
        ro = (j < len(s) and s[j] == "_")
        if (lo or ro) and not (lo and ro):
            score = max(score, 20)
    if _ONE.search(s):
        score = max(score, 5)
    return score


def _line_string(board, r, c, dr, dc, color):
    """构造经过 (r,c) 的整条线字符串，并把 (r,c) 视为已落 color。"""
    sr, sc = r, c
    while _in_bounds(sr - dr, sc - dc):
        sr -= dr
        sc -= dc
    chars = []
    rr, cc = sr, sc
    while _in_bounds(rr, cc):
        if rr == r and cc == c:
            chars.append("O")
        else:
            v = board[rr][cc]
            chars.append("O" if v == color else ("_" if v == EMPTY else "X"))
        rr += dr
        cc += dc
    return "".join(chars)


def _move_score(board, r, c, color) -> int:
    """评估在 (r,c) 落 color 的即时价值（四方向求和）。"""
    board[r][c] = color
    s = 0
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        s += _score_line(_line_string(board, r, c, dr, dc, color))
    board[r][c] = EMPTY
    return s


def _would_win(board, r, c, color) -> bool:
    board[r][c] = color
    w = _is_win_at(board, r, c, color)
    board[r][c] = EMPTY
    return w


def _candidates(board):
    """返回所有「距已有棋子两步内」的空点；空盘则落天元。"""
    stones = [(r, c) for r in range(SIZE) for c in range(SIZE) if board[r][c] != EMPTY]
    if not stones:
        return [(SIZE // 2, SIZE // 2)]
    res = set()
    for r, c in stones:
        for dr in (-2, -1, 0, 1, 2):
            for dc in (-2, -1, 0, 1, 2):
                nr, nc = r + dr, c + dc
                if _in_bounds(nr, nc) and board[nr][nc] == EMPTY:
                    res.add((nr, nc))
    return list(res)


def _top_candidates(board, color, k):
    """按即时价值排序取前 k 个候选（供搜索剪枝）。"""
    opp = WHITE if color == BLACK else BLACK
    scored = []
    for r, c in _candidates(board):
        sc = _move_score(board, r, c, color) + 0.9 * _move_score(board, r, c, opp)
        scored.append((sc, r, c))
    scored.sort(reverse=True)
    return [(r, c) for _, r, c in scored[:k]]


def _board_score(board, color) -> int:
    """整盘评估：枚举所有最大连线，累加形状分。"""
    total = 0
    for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
        for r in range(SIZE):
            for c in range(SIZE):
                if _in_bounds(r - dr, c - dc):
                    continue  # 仅从每条线起点开始
                chars = []
                rr, cc = r, c
                while _in_bounds(rr, cc):
                    v = board[rr][cc]
                    chars.append("O" if v == color else ("_" if v == EMPTY else "X"))
                    rr += dr
                    cc += dc
                total += _score_line("".join(chars))
    return total


def _eval_persp(board, perspective, ai_color, opp) -> int:
    my = perspective
    other = opp if perspective == ai_color else ai_color
    return _board_score(board, my) - _board_score(board, other)


def _negamax(board, depth, alpha, beta, to_move, ai_color, opp, k):
    if depth == 0:
        return _eval_persp(board, to_move, ai_color, opp)
    cands = _top_candidates(board, to_move, k)
    if not cands:
        return _eval_persp(board, to_move, ai_color, opp)
    for r, c in cands:  # 立即取胜优先
        if _would_win(board, r, c, to_move):
            return _WIN_SCORE + depth
    best = -_INF
    other = opp if to_move == ai_color else ai_color
    for r, c in cands:
        board[r][c] = to_move
        val = -_negamax(board, depth - 1, -beta, -alpha, other, ai_color, opp, k)
        board[r][c] = EMPTY
        if val > best:
            best = val
        if best > alpha:
            alpha = best
        if alpha >= beta:
            break
    return best


def _greedy(board, ai_color, opp, cands, lookahead):
    best, best_score = None, -_INF
    for r, c in cands:
        atk = _move_score(board, r, c, ai_color)
        dfs = _move_score(board, r, c, opp)
        score = atk + 0.9 * dfs
        if lookahead:
            board[r][c] = ai_color
            opp_best = 0
            for r2, c2 in _top_candidates(board, opp, 8):
                v = _move_score(board, r2, c2, opp) + 0.9 * _move_score(board, r2, c2, ai_color)
                if v > opp_best:
                    opp_best = v
            board[r][c] = EMPTY
            score = atk + 0.9 * dfs - 0.9 * opp_best
        if score > best_score:
            best_score, best = score, (r, c)
    return best


def ai_move(board, ai_color, human_color, difficulty):
    """根据难度返回 AI 的落子坐标 (row, col)。"""
    opp = human_color
    cands = _candidates(board)
    if not cands:
        return (SIZE // 2, SIZE // 2)
    # 1) 自己能赢就赢
    for r, c in cands:
        if _would_win(board, r, c, ai_color):
            return (r, c)
    # 2) 对手要赢就堵
    for r, c in cands:
        if _would_win(board, r, c, opp):
            return (r, c)

    if difficulty == "简单":
        return random.choice(cands)
    if difficulty in ("普通", "困难"):
        return _greedy(board, ai_color, opp, cands, lookahead=(difficulty == "困难"))
    # 大师：极小化极大搜索
    k = 10
    depth = 3
    best, best_val = None, -_INF
    for r, c in _top_candidates(board, ai_color, k):
        board[r][c] = ai_color
        val = -_negamax(board, depth - 1, -_INF, _INF, opp, ai_color, opp, k)
        board[r][c] = EMPTY
        if val > best_val:
            best_val, best = val, (r, c)
    return best or random.choice(cands)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _build_html(board, current, players, last_move, winner=None,
                subtitle="双人对战"):
    cells = []
    for r in range(SIZE):
        for c in range(SIZE):
            v = board[r][c]
            inner = ""
            if v == BLACK:
                inner = '<div class="stone black"></div>'
            elif v == WHITE:
                inner = '<div class="stone white"></div>'
            if last_move and last_move[0] == c and last_move[1] == r:
                inner += '<div class="lastmark"></div>'
            cells.append(f'<div class="cell">{inner}</div>')
    if winner:
        turn = f"🎉 {winner} 五子连珠，获胜！"
    else:
        if current == BLACK:
            color_name = "黑"
        elif current == WHITE:
            color_name = "白"
        else:
            color_name = ""
        who = players.get(current, "对方")
        turn = f"轮到 {who}（{color_name}子）落子"
    return (
        _CARD_TEMPLATE
        .replace("__CELLS__", "".join(cells))
        .replace("__TURN__", turn)
        .replace("__SUBTITLE__", subtitle)
    )


def _build_text(board, players, last_move, winner=None, subtitle="双人对战"):
    head = "   " + " ".join(chr(65 + i) for i in range(SIZE))
    lines = [subtitle, head]
    for r in range(SIZE):
        row = "".join(
            "●" if board[r][c] == BLACK else "○" if board[r][c] == WHITE else "·"
            for c in range(SIZE)
        )
        lines.append(f"{r + 1:2} {row}")
    if winner:
        lines.append(f"\n🎉 {winner} 五子连珠，获胜！")
    else:
        lines.append("\n发送「落子 列 行」下棋，例：落子 A3")
    return "\n".join(lines)


async def _render(ctx, game, winner=None):
    """渲染棋盘（图片优先，失败回退文本）。"""
    subtitle = game.get("subtitle", "双人对战")
    renderer = ctx.gateway.card_renderer
    if renderer is not None:
        inner = _build_html(
            game["board"], game["current"], game["players"],
            game["last_move"], winner=winner, subtitle=subtitle,
        )
        html = ctx.gateway.glass_wrap(inner, width=560)
        ok = await ctx.gateway.send_card(html, ctx.msg_id)
        if ok:
            return ctx.IMAGE_SENT
    return _build_text(game["board"], game["players"], game["last_move"],
                       winner=winner, subtitle=subtitle)


# ---------------------------------------------------------------------------
# 指令解析辅助
# ---------------------------------------------------------------------------
def _resolve_difficulty(text: str):
    """从「五子棋 人机 Xxx」中解析难度键，缺省返回默认。"""
    parts = text.strip().split()
    for p in parts[2:]:
        key = _ALIAS_TO_KEY.get(p.lower())
        if key:
            return key
    return DEFAULT_DIFFICULTY


def _is_ai_start(text: str) -> bool:
    t = text.strip().lower()
    if t in ("五子棋 人机", "五子棋 ai", "五子棋 单人", "五子棋 人机对战"):
        return True
    # 形如「五子棋 人机 大师」等
    parts = text.strip().split()
    if len(parts) >= 2 and parts[0] == "五子棋" and parts[1] in ("人机", "ai", "单人", "人机对战"):
        return True
    if t.replace(" ", "") in ("五子棋人机", "五子棋ai", "五子棋单人"):
        return True
    return False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def handle_message(ctx):
    text = ctx.content.strip()
    group = ctx.group_openid

    # ---- 帮助 / 难度说明 ----
    if text in ("五子棋 帮助", "五子棋 指令", "五子棋 help", "gobang", "gobang help", "五子棋玩法"):
        return _HELP_TEXT
    if text in ("五子棋 难度", "五子棋 难度表", "五子棋 难度说明"):
        return _DIFFICULTY_HELP

    # ---- 开局：双人 ----
    if text in ("五子棋", "五子棋 开始", "五子棋 双人", "五子棋 pvp", "五子棋 双人对战"):
        if group in GAMES:
            return "⚠️ 本群已有一局进行中，发送「五子棋 结束」可重开。"
        GAMES[group] = {
            "board": _new_board(),
            "current": BLACK,
            "last_key": None,
            "players": {},
            "last_move": None,
            "mode": "pvp",
            "difficulty": None,
            "human_color": BLACK,
            "ai_color": None,
            "ai_name": None,
            "subtitle": "双人对战",
        }
        GAMES[group]["players"][BLACK] = ctx.sender_name
        return (
            "♟️ 五子棋开局（双人对战）！你执黑先行。\n"
            "发送「落子 A3」（或「落子 1 3」）落子，对方落子即自动执白。\n"
            "率先五子连珠者获胜。需要人机对战请发「五子棋 人机」。"
        )

    # ---- 开局：人机 ----
    if _is_ai_start(text):
        if group in GAMES:
            return "⚠️ 本群已有一局进行中，发送「五子棋 结束」可重开。"
        difficulty = _resolve_difficulty(text)
        ai_name = f"AI·{difficulty}"
        GAMES[group] = {
            "board": _new_board(),
            "current": BLACK,
            "last_key": None,
            "players": {},
            "last_move": None,
            "mode": "ai",
            "difficulty": difficulty,
            "human_color": BLACK,
            "ai_color": WHITE,
            "ai_name": ai_name,
            "subtitle": f"人机对战 · {ai_name}",
        }
        GAMES[group]["players"][BLACK] = ctx.sender_name
        return (
            f"🤖 人机开局！难度【{difficulty}】，你执黑先行，{ai_name} 执白。\n"
            "发送「落子 A3」（或「落子 1 3」）落子，AI 会自动应手。\n"
            "率先五子连珠者获胜。"
        )

    # ---- 认输 ----
    if text in ("五子棋 认输", "五子棋 投降"):
        game = GAMES.get(group)
        if game is None:
            return "⚠️ 当前没有进行中的对局。"
        if game["mode"] == "ai":
            GAMES.pop(group, None)
            return f"🏳️ {ctx.sender_name} 认输，{game['ai_name']} 获胜！"
        players = game["players"]
        if ctx.sender_name == players.get(BLACK):
            loser_color = BLACK
        elif ctx.sender_name == players.get(WHITE):
            loser_color = WHITE
        else:
            loser_color = game["current"]
        winner_color = WHITE if loser_color == BLACK else BLACK
        winner = players.get(winner_color) or "对方"
        GAMES.pop(group, None)
        return f"🏳️ {ctx.sender_name} 认输，{winner} 获胜！"

    # ---- 结束 ----
    if text in ("五子棋 结束", "五子棋 停"):
        game = GAMES.pop(group, None)
        if game is None:
            return "⚠️ 当前没有进行中的对局。"
        return "🛑 本局五子棋已结束。"

    # ---- 落子 ----
    if text.startswith("落子"):
        game = GAMES.get(group)
        if game is None:
            return "⚠️ 请先发送「五子棋」或「五子棋 人机」开始一局。"
        move = _parse_move(text)
        if move is None:
            return "⚠️ 坐标无法识别。请用「落子 A3」或「落子 1 3」（列 A~O，行 1~15）。"
        row, col = move
        if not (0 <= col < SIZE and 0 <= row < SIZE):
            return "⚠️ 坐标超出 15×15 棋盘范围。"
        if game["board"][row][col] != EMPTY:
            return "⚠️ 该位置已有棋子，换一个吧。"

        # 人机模式：仅允许轮到人类时落子
        if game["mode"] == "ai" and game["current"] != game["human_color"]:
            return "⏳ 等待 AI 落子中，请稍候……"

        color = game["current"]
        game["board"][row][col] = color
        game["players"].setdefault(color, ctx.sender_name)
        game["last_key"] = ctx.sender_openid or ctx.sender_name
        game["last_move"] = (col, row)

        # 人类落子判胜
        if _is_win_at(game["board"], row, col, color):
            winner = game["players"][color]
            GAMES.pop(group, None)
            return await _render(ctx, game, winner=winner)
        if _board_full(game["board"]):
            GAMES.pop(group, None)
            return await _render(ctx, game, winner="平局（棋盘已满）")

        # 人机模式：AI 应手
        if game["mode"] == "ai":
            ai_color = game["ai_color"]
            human_color = game["human_color"]
            game["current"] = ai_color
            ar, ac = ai_move(game["board"], ai_color, human_color, game["difficulty"])
            game["board"][ar][ac] = ai_color
            game["players"].setdefault(ai_color, game["ai_name"])
            game["last_move"] = (ac, ar)
            if _is_win_at(game["board"], ar, ac, ai_color):
                winner = game["ai_name"]
                GAMES.pop(group, None)
                return await _render(ctx, game, winner=winner)
            if _board_full(game["board"]):
                GAMES.pop(group, None)
                return await _render(ctx, game, winner="平局（棋盘已满）")
            game["current"] = human_color

        return await _render(ctx, game)

    return None
