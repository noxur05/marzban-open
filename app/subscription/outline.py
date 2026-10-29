import json


# Provider-icon priority for the Outline exit, best first. These are the
# vpn-watchdog `provider_icon` emojis: 🍃 Leaf Cloud, 🔥 Beget, ⚡ TimeWeb — the
# only provider signal a Marzban host carries (its remark ends "…{icon×n}").
# Kept as a small constant so re-skinning an icon upstream is a one-line change.
OUTLINE_PROVIDER_PRIORITY = ("🍃", "🔥", "⚡")

# A host's exit is Russia iff its remark carries the 🇷🇺 flag. The bot labels a
# host by its TRUE exit — a Russia box used as a DNAT front shows its (non-
# Russia) destination flag, a Russia direct-node shows 🇷🇺 — so the flag alone
# separates "Russia-as-front (fine)" from "Russia exit (avoid)".
RUSSIA_FLAG = "🇷🇺"


class OutlineConfiguration:
    def __init__(self):
        # Outline keys are single-server. We pick ONE shadowsocks host as the
        # exit — the best-ranked (see _rank) across the user's visible SS hosts —
        # instead of "whichever host was processed last".
        self.config = {}
        # Rank of the host currently in self.config (lower = better); None until
        # the first SS host is seen.
        self._best_rank = None

    def add_directly(self, data: dict):
        self.config = data

    def render(self, reverse=False):
        if reverse:
            items = list(self.config.items())
            items.reverse()
            self.config = dict(items)
        return json.dumps(self.config, indent=0)

    def make_outbound(
        self, remark: str, address: str, port: int, password: str, method: str
    ):
        # Outline dynamic-key configs are validated strictly (parsed as
        # YAML) and reject unknown fields — only server/server_port/
        # password/method (+ optional prefix) are allowed. A `tag`/`remark`
        # key breaks the client with `unknown field "tag"`. The access-key
        # name comes from the ssconf:// URL fragment, not the body.
        config = {
            "method": method,
            "password": password,
            "server": address,
            "server_port": port,
        }
        return config

    @staticmethod
    def _rank(remark: str) -> int:
        """Selection rank for a shadowsocks host, LOWER = preferred:
          0..N-1  a preferred provider, non-Russia (Leaf → Beget → TimeWeb)
          N       any other non-Russia host (fallback)
          N+1     a Russia exit (🇷🇺) — last resort, used only when the user has
                  no non-Russia host at all, so the key is never empty."""
        n = len(OUTLINE_PROVIDER_PRIORITY)
        if RUSSIA_FLAG in remark:
            return n + 1
        for i, icon in enumerate(OUTLINE_PROVIDER_PRIORITY):
            if icon in remark:
                return i
        return n

    def add(self, remark: str, address: str, inbound: dict, settings: dict):
        if inbound["protocol"] != "shadowsocks":
            return

        # Keep only the best-ranked SS host; on a tie the FIRST seen wins.
        rank = self._rank(remark)
        if self._best_rank is not None and rank >= self._best_rank:
            return
        self._best_rank = rank

        outbound = self.make_outbound(
            remark=remark,
            address=address,
            port=inbound["port"],
            password=settings["password"],
            method=settings["method"],
        )
        self.add_directly(outbound)