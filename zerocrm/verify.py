"""The two-stage verify gate — the load-bearing protection for warming domains.

MillionVerifier grades most addresses cheaply; only its `catch_all` (or, in the
ZeroBounce-only path, a catch-all status) escalates to ZeroBounce's AI confidence
score, which is the one signal that grades an un-provable catch-all mailbox. An
address only becomes a candidate on a `send` decision — nothing unverified ever
reaches a sequence.

Either provider alone works (whichever is configured). With neither, check()
returns `risky`, so a missing verifier fails CLOSED (never auto-sends).
"""

from __future__ import annotations


class Verifier:
    def __init__(self, *, millionverifier=None, zerobounce=None, catchall_min_score: int = 7):
        self.mv = millionverifier
        self.zb = zerobounce
        self.catchall_min_score = catchall_min_score

    def check(self, email: str) -> tuple[str, dict]:
        """Return (decision, evidence). decision in send | block | risky."""
        if self.mv is not None:
            res = self.mv.validate(email)
            verdict = self.mv.classify(res)          # send | block | catchall | risky
            if verdict == "catchall":
                return self._score_catchall(email, {"millionverifier": res})
            decision = verdict if verdict in ("send", "block") else "risky"
            return decision, {"millionverifier": res}

        if self.zb is not None:
            from .drivers.zerobounce import classify
            res = self.zb.validate(email)
            if (res.get("status") or "").lower() == "catch-all":
                return self._score_catchall(email, {"zerobounce": res})
            return classify(res), {"zerobounce": res}   # send | block | risky

        return "risky", {}                            # no verifier -> fail closed

    def _score_catchall(self, email: str, evidence: dict) -> tuple[str, dict]:
        """Grade a catch-all by ZeroBounce confidence; below threshold -> risky
        (held, not sent). Without a scorer a catch-all is risky by default."""
        if self.zb is None:
            return "risky", evidence
        score = self.zb.score(email)
        evidence["zerobounce_score"] = score
        return ("send" if score >= self.catchall_min_score else "risky"), evidence
