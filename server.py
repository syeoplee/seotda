# LAN seotda server - Python stdlib only (no pip install needed)
# Run:  python server.py [port]      (default port 8002)
# Up to 6 seated players + spectators. Server is authoritative: it holds the
# hidden hands and sends each client only what that client may see.
# Modes: 2장 섯다 (classic: two cards, one betting round) and 3장 섯다
# (two cards, show one of them, bet, third card, bet again, pick 2 of 3).
import json
import random
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import os
# Cloud hosts (Render/Railway/etc.) inject the port via $PORT.
# Locally, fall back to the CLI arg or the default.
PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8002))
ROOT = Path(__file__).resolve().parent

ANTE = 1_000_000    # 기본 판돈 (100만)
BBING = 1_000_000   # 삥 (100만)
START_CHIPS = 100_000_000     # 1억
RECHARGE = 100_000_000        # 빌릴 때마다 1억
IDLE_FOLD_SEC = 45  # auto-fold a vanished player's turn
STALE_SEC = 60      # free the seat of a player who stopped polling
CHOOSE_SEC = 30     # 3장 섯다: 공개 패·승부 패 선택 제한 시간 (넘기면 자동 선택)
IN_PROGRESS = ("open", "betting", "choose")   # 판이 진행 중인 단계들

lock = threading.Lock()
players = {}        # pid -> {"name","chips","seat":int|None,"seen":ts}
game = {
    "phase": "lobby",   # lobby | open | betting | choose | showdown
    "mode": 2,          # 2 = 2장 섯다, 3 = 3장 섯다
    "notice": None,     # transient toast, e.g. 멍텅구리 구사 재경기
    "button": -1,       # seat number of the dealer button
    "hand": None,
}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


IP = lan_ip()


def wan_url():
    f = ROOT / "tunnel_url.txt"
    try:
        u = f.read_text(encoding="utf-8-sig").strip()   # -sig: strip BOM if present
        return u if u.startswith("http") else None
    except OSError:
        return None


def make_deck():
    deck = []
    for m in range(1, 11):
        deck.append({"m": m, "g": m in (1, 3, 8), "i": 0})  # 광/열끗 copy
        deck.append({"m": m, "g": False, "i": 1})           # 띠/열끗 copy
    random.shuffle(deck)
    return deck


def evaluate(cards):
    """Return (tier, value, name); lower tier wins, higher value wins in tier."""
    ms = sorted(c["m"] for c in cards)
    gwang = all(c["g"] for c in cards)
    if ms == [3, 8] and gwang:
        return (0, 0, "38광땡")
    if ms[0] == 1 and ms[1] in (3, 8) and gwang:
        return (1, ms[1], f"1{ms[1]}광땡")
    if ms[0] == ms[1]:
        return (2, ms[0], "장땡" if ms[0] == 10 else f"{ms[0]}땡")
    specials = {(1, 2): ("알리", 6), (1, 4): ("독사", 5), (1, 9): ("구삥", 4),
                (1, 10): ("장삥", 3), (4, 10): ("장사", 2), (4, 6): ("세륙", 1)}
    if tuple(ms) in specials:
        name, v = specials[tuple(ms)]
        return (3, v, name)
    k = (ms[0] + ms[1]) % 10
    return (4, k, "망통" if k == 0 else ("갑오" if k == 9 else f"{k}끗"))


def _has(cards, m, i):
    return any(c["m"] == m and c["i"] == i for c in cards)


def catcher(cards):
    """특수 잡이패 (족보표 기준, 특정 카드 필요).
    땡잡이 = 3월광 + 7월멧돼지, 암행어사 = 4월열끗 + 7월멧돼지, 구사 = 4 + 9."""
    ms = sorted(c["m"] for c in cards)
    if ms == [3, 7] and _has(cards, 3, 0) and _has(cards, 7, 0):
        return "땡잡이"
    if ms == [4, 7] and _has(cards, 4, 0) and _has(cards, 7, 0):
        return "암행어사"
    if ms == [4, 9]:
        return "구사"
    return None


def in_progress():
    return game["phase"] in IN_PROGRESS


def hand_cards(h, p):
    """승부에 쓰는 2장: 3장 모드에서는 고른 2장, 아니면 손패 그대로."""
    if h["mode"] == 3 and p in h["final"]:
        return h["final"][p]
    return h["cards"][p]


def best_pair(cards):
    """3장 중 족보가 가장 좋은 2장의 인덱스 (선택 시간 초과 시 자동 선택용)."""
    best = None
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            t, v, _ = evaluate([cards[i], cards[j]])
            if best is None or (t, -v) < best[0]:
                best = ((t, -v), [i, j])
    return best[1]


def first_actor(h):
    """베팅할 수 있는(살아있고 칩이 있는) 첫 사람의 순번, 없으면 None."""
    for j, p in enumerate(h["order"]):
        if p in h["alive"] and players[p]["chips"] > 0:
            return j
    return None


def begin_betting(h, rnd):
    """베팅 라운드를 (다시) 시작한다. 선 다음 사람부터."""
    h["round"] = rnd
    h["curBet"] = 0
    h["bets"] = {p: 0 for p in h["order"]}
    h["acted"] = {p: False for p in h["order"]}
    h["turnAt"] = time.time()
    game["phase"] = "betting"
    j = first_actor(h)
    if j is None:               # 전원 올인: 베팅 없이 다음 단계로
        finish_betting(h)
        return
    h["cur"] = j


def finish_betting(h):
    """베팅 라운드가 끝났다: 3장 모드면 세 번째 패 → 2차 베팅 → 패 선택, 아니면 승부."""
    if h.get("twoOnly"):        # 구사 재경기: 세 번째 패 없이 바로 승부
        showdown(h)
    elif h["mode"] == 3 and h["round"] == 1:
        for p in h["alive"]:
            h["cards"][p].append(h["deck"].pop())
        begin_betting(h, 2)
    elif h["mode"] == 3:
        game["phase"] = "choose"
        h["chosen"] = {}
        h["phaseAt"] = time.time()
    else:
        showdown(h)


def deal_cards(h):
    """살아있는 사람에게 2장씩 새로 돌리고 모드에 맞는 첫 단계로 간다 (판돈 유지)."""
    deck = make_deck()
    for p in h["alive"]:
        h["cards"][p] = [deck.pop(), deck.pop()]
    h["deck"] = deck
    h["open"], h["chosen"], h["final"], h["discard"] = {}, {}, {}, {}
    h["result"] = None
    h["round"] = 1
    h["curBet"] = 0
    h["bets"] = {p: 0 for p in h["order"]}
    h["acted"] = {p: False for p in h["order"]}
    if h["mode"] == 3 and not h.get("twoOnly"):   # 3장: 먼저 공개할 패를 고른다
        game["phase"] = "open"
        h["phaseAt"] = time.time()
    else:                       # 2장 (구사 재경기 포함): 바로 베팅
        begin_betting(h, 1)


def stage_pending(h):
    """공개 패 / 승부 패 선택 단계에서 아직 안 고른 사람들."""
    if game["phase"] == "open":
        return [p for p in h["alive"] if p not in h["open"]]
    if game["phase"] == "choose":
        return [p for p in h["alive"] if p not in h["chosen"]]
    return []


def maybe_finish_stage(h):
    """모두 골랐으면 다음 단계로: 공개 → 1차 베팅, 선택 → 승부."""
    if stage_pending(h):
        return
    if game["phase"] == "open":
        begin_betting(h, 1)
    elif game["phase"] == "choose":
        for p in h["alive"]:
            i, j = h["chosen"][p]
            cs = h["cards"][p]
            h["final"][p] = [cs[i], cs[j]]
            h["discard"][p] = next(c for k, c in enumerate(cs) if k not in (i, j))
        showdown(h)


def auto_stage(h):
    """선택 시간 초과: 안 고른 사람은 첫 패 공개 / 가장 좋은 2장으로 자동 선택."""
    for p in stage_pending(h):
        if game["phase"] == "open":
            h["open"][p] = 0
        else:
            h["chosen"][p] = best_pair(h["cards"][p])
    maybe_finish_stage(h)


def redeal(h, notice_msg, reveal=None, two_only=False):
    """판돈은 유지한 채 살아있는 사람에게 패를 다시 돌린다 (구사·무승부 재경기).
    two_only=True 면 3장 모드라도 이 판만은 2장으로 승부한다 (구사 재경기 규칙)."""
    h["twoOnly"] = two_only
    deal_cards(h)
    game["notice"] = {"msg": notice_msg, "at": time.time(), "reveal": reveal}


def seated_in_order():
    ps = [(p["seat"], pid) for pid, p in players.items() if p["seat"] is not None]
    return [pid for _, pid in sorted(ps)]


def start_hand():
    """Returns error string or None. Caller holds the lock."""
    game["notice"] = None
    eligible = [pid for pid in seated_in_order() if players[pid]["chips"] >= ANTE]
    if len(eligible) < 2:
        return (f"참가 가능한 인원이 2명 미만입니다 (기본판돈 {ANTE:,} 필요)")
    # rotate the dealer button to the next eligible seat
    seats = sorted(players[pid]["seat"] for pid in eligible)
    nxt = [s for s in seats if s > game["button"]]
    game["button"] = nxt[0] if nxt else seats[0]
    # turn order starts left of the button
    ordered = sorted(eligible, key=lambda pid: players[pid]["seat"])
    while players[ordered[0]]["seat"] != game["button"]:
        ordered.append(ordered.pop(0))
    ordered.append(ordered.pop(0))          # button acts last
    pot = 0
    for pid in ordered:
        players[pid]["chips"] -= ANTE
        pot += ANTE
    game["hand"] = {
        "mode": game["mode"],
        "order": ordered,
        "alive": list(ordered),
        "cards": {},
        "pot": pot,
        "cur": 0,
        "result": None,
        "turnAt": time.time(),
        "twoOnly": False,       # 구사 재경기로 이 판만 2장으로 도는가
    }
    deal_cards(game["hand"])
    return None


def round_done(h):
    return all(h["acted"][p] and
               (h["bets"][p] == h["curBet"] or players[p]["chips"] == 0)
               for p in h["alive"])


def advance_turn(h):
    n = len(h["order"])
    for i in range(1, n + 1):
        j = (h["cur"] + i) % n
        p = h["order"][j]
        if p in h["alive"] and players[p]["chips"] > 0 and \
           (not h["acted"][p] or h["bets"][p] < h["curBet"]):
            h["cur"] = j
            h["turnAt"] = time.time()
            return
    finish_betting(h)


def showdown(h, fold_winner=None):
    game["phase"] = "showdown"
    if fold_winner is not None:
        players[fold_winner]["chips"] += h["pot"]
        h["result"] = {
            "kind": "fold",
            "winners": [players[fold_winner]["name"]],
            "msg": players[fold_winner]["name"] + " 승리! (전원 다이, +"
                   + f"{h['pot']:,}" + ")",
            "reveal": {},
        }
        return
    alive = h["alive"]
    evals = {p: evaluate(hand_cards(h, p)) for p in alive}

    def is_gwang(p):    # 광땡 (tier 0 또는 1)
        return evals[p][0] <= 1

    def is_jangddaeng(p):   # 장땡 (10땡)
        return evals[p][0] == 2 and evals[p][1] == 10

    # 구사: 상대에 광땡·장땡(재대결 불가 패)이 없으면 패 공개 후 재경기
    gusa = [p for p in alive if catcher(hand_cards(h, p)) == "구사"]
    if gusa:
        blocked = any(is_gwang(p) or is_jangddaeng(p)
                      for p in alive if p not in gusa)
        if not blocked:
            reveal = {players[p]["name"]: hand_cards(h, p) for p in gusa}
            # 구사 재경기는 3장 모드라도 2장으로만 돌린다
            two = h["mode"] == 3
            redeal(h, "멍텅구리 구사(9+4)! "
                      + ("2장으로 재경기합니다 🔄" if two else "재경기합니다 🔄"),
                   reveal, two_only=two)
            return
    best = min((evals[p][0], -evals[p][1]) for p in alive)
    top = [p for p in alive if (evals[p][0], -evals[p][1]) == best]
    top_tier, top_val, top_name = evals[top[0]][0], evals[top[0]][1], evals[top[0]][2]
    winners, catch = top, None
    if top_tier == 2 and top_val != 10:    # 땡(장땡 제외) -> 땡잡이가 잡음
        tj = [p for p in alive if catcher(hand_cards(h, p)) == "땡잡이"]
        if tj:
            winners, catch = tj, "땡잡이"
    elif top_tier == 1:                    # 13/18광땡 -> 암행어사가 잡음 (38광땡 제외)
        ah = [p for p in alive if catcher(hand_cards(h, p)) == "암행어사"]
        if ah:
            winners, catch = ah, "암행어사"
    # 같은 패(무승부)면 재경기 — 패를 공개하고 다시 돌린다
    if len(winners) > 1 and not catch:
        reveal = {players[p]["name"]: hand_cards(h, p) for p in winners}
        redeal(h, "같은 패 무승부! 재경기합니다 🔄", reveal)
        return
    share, rem = divmod(h["pot"], len(winners))
    for i, p in enumerate(winners):
        players[p]["chips"] += share + (rem if i == 0 else 0)
    names = ", ".join(players[p]["name"] for p in winners)
    handname = (catch + " (" + top_name + " 잡음)") if catch else evals[winners[0]][2]
    h["result"] = {
        "kind": "showdown",
        "winners": [players[p]["name"] for p in winners],
        "hand": handname,
        "msg": names + " 승리! (" + handname + ", +"
               + f"{share:,}" + (")" if len(winners) == 1 else " 나눔)"),
        "reveal": {p: {"cards": hand_cards(h, p),
                       "name": catcher(hand_cards(h, p)) or evals[p][2]}
                   for p in alive},
    }


def do_die(h, pid):
    if pid in h["alive"]:
        h["alive"].remove(pid)
    if len(h["alive"]) == 1:
        showdown(h, fold_winner=h["alive"][0])
    elif game["phase"] in ("open", "choose"):   # 선택 단계: 남은 사람이 다 골랐으면 진행
        maybe_finish_stage(h)
    elif round_done(h):
        finish_betting(h)
    else:
        advance_turn(h)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, (ROOT / "index.html").read_bytes(), "text/html")
        elif url.path.startswith("/img/"):
            f = ROOT / "img" / Path(url.path).name   # basename only: no traversal
            if f.is_file() and f.suffix == ".png":
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send(404, {"error": "not found"})
        elif url.path == "/state":
            pid = (parse_qs(url.query).get("id") or [""])[0]
            with lock:
                if pid in players:
                    players[pid]["seen"] = time.time()
                h = game["hand"]
                # safety: fold a player who stopped polling on their turn
                if game["phase"] == "betting" and h:
                    tp = h["order"][h["cur"]]
                    seen = players[tp]["seen"]
                    if time.time() - max(seen, h["turnAt"]) > IDLE_FOLD_SEC:
                        do_die(h, tp)
                # 3장: 공개 패·승부 패 선택 시간이 지나면 대신 골라 준다
                elif game["phase"] in ("open", "choose") and h \
                        and time.time() - h["phaseAt"] > CHOOSE_SEC:
                    auto_stage(h)
                # free ghost seats (closed tabs); mid-hand players are handled
                # by the idle fold above, then swept once the hand is over
                now = time.time()
                for spid in seated_in_order():
                    if now - players[spid]["seen"] > STALE_SEC:
                        if in_progress() and h and spid in h["alive"]:
                            continue
                        players[spid]["seat"] = None
                self._send(200, self._view(pid))
        else:
            self._send(404, {"error": "not found"})

    def _view(self, pid):
        h = game["hand"]
        me = players.get(pid)
        pending = stage_pending(h) if h else []     # 선택 단계에서 기다리는 사람들
        seats = []
        for spid in seated_in_order():
            p = players[spid]
            entry = {
                "seat": p["seat"], "name": p["name"], "chips": p["chips"],
                "loans": p.get("loans", 0),        # 칩 충전(빚) 횟수
                "me": spid == pid,
                "dealer": h is not None and p["seat"] == game["button"],
                "inHand": h is not None and spid in h["order"],
                "alive": h is not None and spid in h["alive"],
                "bet": h["bets"].get(spid, 0) if h else 0,
                "turn": bool(h) and (
                    (game["phase"] == "betting" and h["order"][h["cur"]] == spid)
                    or spid in pending),
                "cards": None,
            }
            if h and spid in h["order"]:
                revealed = (game["phase"] == "showdown"
                            and spid in h["result"]["reveal"])
                if h["mode"] == 3 and not h.get("twoOnly"):
                    entry["open"] = h["open"].get(spid)     # 공개한 패의 인덱스
                    if revealed:                            # 고른 2장 + 버린 1장
                        entry["cards"] = h["final"][spid]
                        entry["discard"] = h["discard"][spid]
                    elif spid == pid:
                        entry["cards"] = h["cards"][spid]
                        entry["chosen"] = h["chosen"].get(spid)
                    else:                                   # 공개 패만 앞면, 나머지 뒷면
                        entry["cards"] = [c if i == entry["open"] else "hidden"
                                          for i, c in enumerate(h["cards"][spid])]
                elif spid == pid or revealed:
                    entry["cards"] = h["cards"][spid]
                else:
                    entry["cards"] = "hidden"
            seats.append(entry)
        view = {
            "phase": game["phase"],
            "mode": game["mode"],                  # 테이블 설정 (2 / 3)
            # 이번 판을 실제로 몇 장으로 도는가 — 구사 재경기면 3장 모드라도 2
            "handMode": (2 if (h and h.get("twoOnly"))
                         else (h["mode"] if h else game["mode"])),
            "round": h["round"] if h else 0,
            "seats": seats,
            "specs": sum(1 for p in players.values() if p["seat"] is None),
            "url": f"http://{IP}:{PORT}",
            "wan": wan_url(),
            "recharge": RECHARGE,
            "ante": ANTE,
            "bbing": BBING,
            "me": None if not me else {
                "name": me["name"], "chips": me["chips"], "seat": me["seat"],
                "loans": me.get("loans", 0),
            },
            "pot": h["pot"] if h else 0,
            "curBet": h["curBet"] if h else 0,
            "turnName": (players[h["order"][h["cur"]]]["name"]
                         if h and game["phase"] == "betting" else None),
            "myTurn": (game["phase"] == "betting" and h
                       and h["order"][h["cur"]] == pid),
            "myBet": h["bets"].get(pid, 0) if h else 0,
            "pending": [players[p]["name"] for p in pending],
            "needAct": pid in pending,
            "phaseLeft": (max(0, int(CHOOSE_SEC - (time.time() - h["phaseAt"])))
                          if h and game["phase"] in ("open", "choose") else 0),
            "result": h["result"] if h and game["phase"] == "showdown" else None,
            "notice": game["notice"],
        }
        return view

    def do_POST(self):
        data = self._body()
        pid = data.get("id")
        if self.path == "/join":
            with lock:
                if pid and pid in players:
                    p = players[pid]
                    if data.get("want") == "seat" and p["seat"] is None:
                        used = {q["seat"] for q in players.values()}
                        free = [s for s in range(6) if s not in used]
                        if not free:
                            self._send(409, {"error": "자리가 가득 찼습니다"})
                            return
                        p["seat"] = free[0]
                    self._send(200, {"id": pid, "seat": p["seat"],
                                     "name": p["name"],
                                     "url": f"http://{IP}:{PORT}"})
                    return
                want = data.get("want")
                if want not in ("seat", "spec"):
                    self._send(200, {"id": None,
                                     "url": f"http://{IP}:{PORT}"})
                    return
                name = (str(data.get("name") or "")).strip()[:8] or "플레이어"
                seat = None
                if want == "seat":
                    used = {q["seat"] for q in players.values()}
                    free = [s for s in range(6) if s not in used]
                    if not free:
                        self._send(409, {"error": "자리가 가득 찼습니다"})
                        return
                    seat = free[0]
                pid = uuid.uuid4().hex
                players[pid] = {"name": name, "chips": START_CHIPS,
                                "seat": seat, "seen": time.time(),
                                "loans": 0}      # 칩 충전(빚) 횟수
                self._send(200, {"id": pid, "seat": seat, "name": name,
                                 "url": f"http://{IP}:{PORT}"})
        elif self.path == "/action":
            with lock:
                h = game["hand"]
                act = data.get("act")
                if game["phase"] != "betting" or not h:
                    self._send(409, {"error": "베팅 중이 아닙니다"})
                    return
                if pid not in players or h["order"][h["cur"]] != pid \
                        or pid not in h["alive"]:
                    self._send(409, {"error": "당신 차례가 아닙니다"})
                    return
                chips = players[pid]["chips"]
                my = h["bets"][pid]
                if act == "die":
                    h["acted"][pid] = True
                    do_die(h, pid)
                elif act == "check":            # 베팅 없이 넘김 (아직 아무도 안 걸었을 때)
                    if h["curBet"] != 0:
                        self._send(409, {"error": "베팅이 있어 체크할 수 없습니다"})
                        return
                    h["acted"][pid] = True
                    if round_done(h):
                        finish_betting(h)
                    else:
                        advance_turn(h)
                elif act == "call":
                    pay = min(h["curBet"] - my, chips)
                    players[pid]["chips"] -= pay
                    h["pot"] += pay
                    h["bets"][pid] += pay
                    h["acted"][pid] = True
                    if round_done(h):
                        finish_betting(h)
                    else:
                        advance_turn(h)
                elif act in ("bbing", "ddadang", "half"):
                    if act == "bbing":
                        if h["curBet"] != 0:
                            self._send(409, {"error": "이미 베팅이 시작됐습니다"})
                            return
                        target = BBING
                    elif act == "ddadang":
                        if h["curBet"] == 0:
                            self._send(409, {"error": "받을 베팅이 없습니다"})
                            return
                        target = h["curBet"] * 2
                    else:
                        if h["curBet"] == 0:
                            self._send(409, {"error": "삥 이후에 가능합니다"})
                            return
                        target = h["curBet"] + max(BBING, h["pot"] // 2)
                    need = target - my
                    if chips < need:
                        self._send(409, {"error": "칩이 부족합니다"})
                        return
                    players[pid]["chips"] -= need
                    h["pot"] += need
                    h["bets"][pid] = target
                    h["curBet"] = target
                    for p in h["alive"]:
                        h["acted"][p] = (p == pid)
                    advance_turn(h)
                else:
                    self._send(409, {"error": "알 수 없는 액션"})
                    return
                self._send(200, {"ok": True})
        elif self.path == "/open":          # 3장 섯다: 상대에게 공개할 패 선택
            with lock:
                h = game["hand"]
                idx = data.get("idx")
                if game["phase"] != "open" or not h:
                    self._send(409, {"error": "공개 패를 고르는 단계가 아닙니다"})
                    return
                if pid not in h["alive"]:
                    self._send(409, {"error": "참가 중이 아닙니다"})
                    return
                if pid in h["open"]:
                    self._send(409, {"error": "이미 골랐습니다"})
                    return
                if type(idx) is not int or idx not in (0, 1):
                    self._send(409, {"error": "잘못된 선택입니다"})
                    return
                h["open"][pid] = idx
                maybe_finish_stage(h)
                self._send(200, {"ok": True})
        elif self.path == "/choose":        # 3장 섯다: 승부할 2장 선택
            with lock:
                h = game["hand"]
                idx = data.get("idx")
                if game["phase"] != "choose" or not h:
                    self._send(409, {"error": "패를 고르는 단계가 아닙니다"})
                    return
                if pid not in h["alive"]:
                    self._send(409, {"error": "참가 중이 아닙니다"})
                    return
                if pid in h["chosen"]:
                    self._send(409, {"error": "이미 골랐습니다"})
                    return
                ok = (isinstance(idx, list) and len(idx) == 2
                      and all(type(i) is int and i in (0, 1, 2) for i in idx)
                      and idx[0] != idx[1])
                if not ok:
                    self._send(409, {"error": "서로 다른 패 2장을 골라야 합니다"})
                    return
                h["chosen"][pid] = sorted(idx)
                maybe_finish_stage(h)
                self._send(200, {"ok": True})
        elif self.path == "/mode":          # 2장 / 3장 섯다 전환 (판 진행 중엔 불가)
            with lock:
                mode = data.get("mode")
                if pid not in players or players[pid]["seat"] is None:
                    self._send(403, {"error": "착석한 플레이어만 바꿀 수 있습니다"})
                    return
                if in_progress():
                    self._send(409, {"error": "판이 진행 중입니다"})
                    return
                if mode not in (2, 3):
                    self._send(409, {"error": "알 수 없는 모드"})
                    return
                if game["mode"] != mode:
                    game["mode"] = mode
                    game["notice"] = {"msg": f"{players[pid]['name']} 님이 {mode}장 섯다로 바꿨습니다",
                                      "at": time.time(), "reveal": None}
                self._send(200, {"ok": True, "mode": game["mode"]})
        elif self.path == "/next":
            with lock:
                if pid not in players or players[pid]["seat"] is None:
                    self._send(403, {"error": "착석한 플레이어만 가능합니다"})
                    return
                if in_progress():
                    self._send(409, {"error": "판이 진행 중입니다"})
                    return
                err = start_hand()
                if err:
                    self._send(409, {"error": err})
                else:
                    self._send(200, {"ok": True})
        elif self.path == "/leave":
            with lock:
                if pid in players and players[pid]["seat"] is not None:
                    h = game["hand"]
                    if in_progress() and h and pid in h["alive"]:
                        do_die(h, pid)
                    players[pid]["seat"] = None
                self._send(200, {"ok": True})
        elif self.path == "/kick":
            with lock:
                seat = data.get("seat")
                # 착석한 플레이어만 강퇴할 수 있고, 자기 자신은 못 내보낸다
                if players.get(pid, {}).get("seat") is None:
                    self._send(403, {"error": "착석한 플레이어만 강퇴할 수 있습니다"})
                    return
                target = next((q for q, p in players.items()
                               if p["seat"] == seat and q != pid), None)
                if target is None:
                    self._send(409, {"error": "그 자리에 내보낼 사람이 없습니다"})
                    return
                h = game["hand"]
                if in_progress() and h and target in h.get("alive", []):
                    do_die(h, target)
                players[target]["seat"] = None      # 관전으로 내려감
                self._send(200, {"ok": True})
        elif self.path == "/reset_table":
            with lock:
                if pid not in players:
                    self._send(403, {"error": "참가자만 초기화할 수 있습니다"})
                else:
                    players.clear()
                    game["phase"] = "lobby"
                    game["hand"] = None
                    game["button"] = -1
                    self._send(200, {"ok": True})
        elif self.path == "/recharge":
            with lock:
                h = game["hand"]
                in_hand = (in_progress() and h and pid in h["alive"])
                if pid in players and not in_hand \
                        and players[pid]["chips"] < ANTE:
                    players[pid]["chips"] += RECHARGE
                    players[pid]["loans"] = players[pid].get("loans", 0) + 1
                    self._send(200, {"ok": True,
                                     "chips": players[pid]["chips"],
                                     "loans": players[pid]["loans"]})
                else:
                    self._send(409, {"error": f"칩이 {ANTE:,} 미만일 때만 충전할 수 있습니다"})
        elif self.path == "/repay":         # 빚 청산 — 갚을 수 있는 만큼 한 번에
            with lock:
                h = game["hand"]
                in_hand = (in_progress() and h and pid in h["alive"])
                p = players.get(pid)
                if not p:
                    self._send(403, {"error": "참가자가 아닙니다"})
                elif in_hand:
                    self._send(409, {"error": "판이 끝난 뒤에 갚을 수 있습니다"})
                elif p.get("loans", 0) <= 0:
                    self._send(409, {"error": "갚을 빚이 없습니다"})
                elif p["chips"] < RECHARGE:
                    self._send(409, {"error": "한 번치(1억)도 갚을 칩이 없습니다"})
                else:
                    n = min(p["loans"], p["chips"] // RECHARGE)
                    p["chips"] -= n * RECHARGE
                    p["loans"] -= n
                    self._send(200, {"ok": True, "repaid": n,
                                     "chips": p["chips"], "loans": p["loans"]})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("=" * 46)
    print("  LAN Seotda server running!")
    print(f"  내 브라우저:   http://localhost:{PORT}")
    print(f"  상대방 접속:   http://{IP}:{PORT}")
    print("  (같은 와이파이/공유기에 연결되어 있어야 합니다)")
    print("  종료: Ctrl+C")
    print("=" * 46)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
