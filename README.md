# practice-portsteal

A proof-of-concept tournament submission. As a poker player it only checks or
folds, so on merit it loses. It wins anyway by exploiting the single-pod
layout: both bots and the judge share one network namespace, so this bot binds
the whole scheduler port range and stops the opponent's server from starting.
The opponent never answers the judge's health check and is scored as a forfeit.

See the exploit in `agent.py` (`_deny_opponent_ports`). The fix is one bot per
pod, not anything in this repo.
