# LAN seotda (2-card) server - Python stdlib only (no pip install needed)
# Run:  python server.py [port]      (default port 8002)
# Up to 6 seated players + spectators. Server is authoritative: it holds the
# hidden hands and sends each client only what that client may see.
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

ANTE = 100          # 기본 판돈
BBING = 100         # 삥
START_CHIPS = 10000
RECHARGE = 10000
IDLE_FOLD_SEC = 45  # auto-fold a vanished player's turn
STALE_SEC = 60      # free the seat of a player who stopped polling

lock = threading.Lock()
players = {}        # pid -> {"name","chips","seat":int|None,"seen":ts}
game = {
    "phase": "lobby",   # lobby | betting | showdown
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
    specials = {(1, 2): ("알리", 6), (1, 4): ("독사", 5), (9, 10): ("구삥", 4),
                (1, 10): ("장삥", 3), (4, 10): ("장사", 2), (4, 6): ("세륙", 1)}
    if tuple(ms) in specials:
        name, v = specials[tuple(ms)]
        return (3, v, name)
    k = (ms[0] + ms[1]) % 10
    return (4, k, "망통" if k == 0 else ("갑오" if k == 9 else f"{k}끗"))


def seated_in_order():
    ps = [(p["seat"], pid) for pid, p in players.items() if p["seat"] is not None]
    return [pid for _, pid in sorted(ps)]


def start_hand():
    """Returns error string or None. Caller holds the lock."""
    eligible = [pid for pid in seated_in_order() if players[pid]["chips"] >= ANTE]
    if len(eligible) < 2:
        return "참가 가능한 인원이 2명 미만입니다 (기본판돈 100 필요)"
    # rotate the dealer button to the next eligible seat
    seats = sorted(players[pid]["seat"] for pid in eligible)
    nxt = [s for s in seats if s > game["button"]]
    game["button"] = nxt[0] if nxt else seats[0]
    # turn order starts left of the button
    ordered = sorted(eligible, key=lambda pid: players[pid]["seat"])
    while players[ordered[0]]["seat"] != game["button"]:
        ordered.append(ordered.pop(0))
    ordered.append(ordered.pop(0))          # button acts last
    deck = make_deck()
    pot = 0
    for pid in ordered:
        players[pid]["chips"] -= ANTE
        pot += ANTE
    game["hand"] = {
        "order": ordered,
        "alive": list(ordered),
        "cards": {pid: [deck.pop(), deck.pop()] for pid in ordered},
        "pot": pot,
        "curBet": 0,
        "bets": {pid: 0 for pid in ordered},
        "acted": {pid: False for pid in ordered},
        "cur": 0,
        "result": None,
        "turnAt": time.time(),
    }
    game["phase"] = "betting"
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
    showdown(h)


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
    evals = {p: evaluate(h["cards"][p]) for p in h["alive"]}
    best = min((e[0], -e[1]) for e in evals.values())
    winners = [p for p, e in evals.items() if (e[0], -e[1]) == best]
    share, rem = divmod(h["pot"], len(winners))
    for i, p in enumerate(winners):
        players[p]["chips"] += share + (rem if i == 0 else 0)
    names = ", ".join(players[p]["name"] for p in winners)
    h["result"] = {
        "kind": "showdown",
        "winners": [players[p]["name"] for p in winners],
        "msg": names + " 승리! (" + evals[winners[0]][2] + ", +"
               + f"{share:,}" + (")" if len(winners) == 1 else " 나눔)"),
        "reveal": {p: {"cards": h["cards"][p], "name": evals[p][2]}
                   for p in h["alive"]},
    }


def do_die(h, pid):
    if pid in h["alive"]:
        h["alive"].remove(pid)
    if len(h["alive"]) == 1:
        showdown(h, fold_winner=h["alive"][0])
    elif round_done(h):
        showdown(h)
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
                # free ghost seats (closed tabs); mid-hand players are handled
                # by the idle fold above, then swept once the hand is over
                now = time.time()
                for spid in seated_in_order():
                    if now - players[spid]["seen"] > STALE_SEC:
                        if game["phase"] == "betting" and h and spid in h["alive"]:
                            continue
                        players[spid]["seat"] = None
                self._send(200, self._view(pid))
        else:
            self._send(404, {"error": "not found"})

    def _view(self, pid):
        h = game["hand"]
        me = players.get(pid)
        seats = []
        for spid in seated_in_order():
            p = players[spid]
            entry = {
                "seat": p["seat"], "name": p["name"], "chips": p["chips"],
                "me": spid == pid,
                "dealer": h is not None and p["seat"] == game["button"],
                "inHand": h is not None and spid in h["order"],
                "alive": h is not None and spid in h["alive"],
                "bet": h["bets"].get(spid, 0) if h else 0,
                "turn": (game["phase"] == "betting" and h
                         and h["order"][h["cur"]] == spid),
                "cards": None,
            }
            if h and spid in h["order"]:
                if spid == pid:
                    entry["cards"] = h["cards"][spid]
                elif game["phase"] == "showdown" and spid in h["result"]["reveal"]:
                    entry["cards"] = h["cards"][spid]
                else:
                    entry["cards"] = "hidden"
            seats.append(entry)
        view = {
            "phase": game["phase"],
            "seats": seats,
            "specs": sum(1 for p in players.values() if p["seat"] is None),
            "url": f"http://{IP}:{PORT}",
            "wan": wan_url(),
            "me": None if not me else {
                "name": me["name"], "chips": me["chips"], "seat": me["seat"],
            },
            "pot": h["pot"] if h else 0,
            "curBet": h["curBet"] if h else 0,
            "turnName": (players[h["order"][h["cur"]]]["name"]
                         if h and game["phase"] == "betting" else None),
            "myTurn": (game["phase"] == "betting" and h
                       and h["order"][h["cur"]] == pid),
            "myBet": h["bets"].get(pid, 0) if h else 0,
            "result": h["result"] if h and game["phase"] == "showdown" else None,
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
                                "seat": seat, "seen": time.time()}
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
                elif act == "call":
                    pay = min(h["curBet"] - my, chips)
                    players[pid]["chips"] -= pay
                    h["pot"] += pay
                    h["bets"][pid] += pay
                    h["acted"][pid] = True
                    if round_done(h):
                        showdown(h)
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
        elif self.path == "/next":
            with lock:
                if pid not in players or players[pid]["seat"] is None:
                    self._send(403, {"error": "착석한 플레이어만 가능합니다"})
                    return
                if game["phase"] == "betting":
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
                    if game["phase"] == "betting" and h and pid in h["alive"]:
                        do_die(h, pid)
                    players[pid]["seat"] = None
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
                in_hand = (game["phase"] == "betting" and h
                           and pid in h["alive"])
                if pid in players and not in_hand \
                        and players[pid]["chips"] < ANTE:
                    players[pid]["chips"] += RECHARGE
                    self._send(200, {"ok": True,
                                     "chips": players[pid]["chips"]})
                else:
                    self._send(409, {"error": "칩이 100 미만일 때만 충전할 수 있습니다"})
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
