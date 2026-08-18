# Name: Anirban Pal
# College: IIEST Shibpur
# Roll Number: 2025ITB029

"""
OracleStrike v4 (hardened)
===========================
Identical strategy logic to v4. Only addition: every public method is
wrapped in try/except, falling back to a safe legal action on any
unforeseen exception. This changes NOTHING on the happy path -- every
call that worked before still calls the exact same _impl code and
returns the exact same result. It only activates if some edge case none
of the 600+ test deals hit would otherwise throw and forfeit the match.
"""

from __future__ import annotations
import random
from typing import Any

POWER_FAIR_VALUES: dict[str, dict[int, float]] = {
    "FORESIGHT":    {1: 0.85, 2: 1.30, 3: 1.65, 4: 2.10, 5: 2.25},
    "TRICK_ROOM":   {1: 1.20, 2: 0.40, 3: 0.40, 4: 0.70, 5: 0.60},
    "SUBSTITUTE":   {1: 1.50, 2: 1.20, 3: 1.00, 4: 0.65, 5: 0.35},
    "STEALTH_ROCK": {1: 1.75, 2: 1.10, 3: 0.80, 4: 0.70, 5: 0.00},
    "TRANSFORM":    {1: 1.60, 2: 1.30, 3: 1.30, 4: 0.00, 5: 0.00},
}

SHIFT_MAGNITUDES: dict[str, int] = {"TRICK_ROOM": 3, "STEALTH_ROCK": 2}
FLAT_HAND_THRESHOLD: int = 2


class Bot:
    name = "OracleStrike_v4"

    def reset(self, seat: int, config: Any, seed: int) -> None:
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        self._opp_k_cache: dict[int, float] = {}
        self._latched: set[int] = set()

    def _opp_k_from_signals(self, obs, live_quote=None, live_turn=None):
        best_round = -1
        best_k = 0.0
        found = False
        for c in obs.contracts:
            if c.maker_seat != self.seat:
                mid = (c.open_bid + c.open_ask) / 2.0
                if c.round > best_round:
                    best_round, best_k, found = c.round, mid, True
        if (live_quote is not None and live_turn == 2 and not obs.is_maker
                and obs.round not in self._latched):
            mid = (live_quote[0] + live_quote[1]) / 2.0
            self._opp_k_cache[obs.round] = mid
            self._latched.add(obs.round)
        for r, k in self._opp_k_cache.items():
            if r > best_round:
                best_round, best_k, found = r, k, True
        return best_k, found

    def _estimate_S(self, obs, live_quote=None, live_turn=None):
        if obs.foresight:
            return float(obs.k_mine) + float(sum(obs.foresight))
        opp_k, has_signal = self._opp_k_from_signals(obs, live_quote, live_turn)
        return float(obs.k_mine) + (opp_k if has_signal else 0.0)

    def _net_shift(self, obs):
        mine = sum(SHIFT_MAGNITUDES.get(p, 0) for p in obs.powers_mine if p in SHIFT_MAGNITUDES)
        theirs = sum(SHIFT_MAGNITUDES.get(p, 0) for p in obs.powers_theirs if p in SHIFT_MAGNITUDES)
        return mine - theirs

    def _get_transform_value(self, obs):
        base = POWER_FAIR_VALUES.get("TRANSFORM", {}).get(obs.round, 0.0)
        if base <= 0:
            return 0.0
        opp_k, has_signal = self._opp_k_from_signals(obs)
        if has_signal and abs(opp_k) > abs(obs.k_mine):
            return base
        if abs(obs.k_mine) <= FLAT_HAND_THRESHOLD:
            return base
        if has_signal and abs(opp_k) <= 2.0:
            return base * 0.4
        return 0.0

    # ── PUBLIC METHODS: safety wrappers around the tested _impl logic ──

    def bid(self, obs, offered):
        try:
            return self._bid_impl(obs, offered)
        except Exception:
            return {}

    def _bid_impl(self, obs, offered):
        if not offered or obs.te_mine <= 0:
            return {}
        name = offered[0]
        r = obs.round
        v_ticks = self._get_transform_value(obs) if name == "TRANSFORM" else POWER_FAIR_VALUES.get(name, {}).get(r, 0.0)
        if v_ticks <= 0.0:
            return {}
        fair_te = v_ticks / self.config.TE_SALVAGE
        opp_te = obs.te_theirs
        rounds_remaining = 6 - r
        reserve = (rounds_remaining - 1) * 3
        max_spendable = max(1, obs.te_mine - reserve)
        if opp_te == 0:
            bid_te = 1
        elif opp_te < fair_te * 0.65:
            bid_te = min(int(opp_te) + 1, int(fair_te * 0.70))
        else:
            shade = 0.55 if r <= 2 else (0.65 if r <= 4 else 0.75)
            bid_te = int(fair_te * shade)
        bid_te = max(0, min(bid_te, max_spendable, obs.te_mine))
        return {name: bid_te} if bid_te > 0 else {}

    def quote(self, obs):
        try:
            return self._quote_impl(obs)
        except Exception:
            w = 4
            try:
                w = obs.final_cap
            except Exception:
                pass
            lo = int(obs.k_mine) - w // 2
            return (lo, lo + w)

    def _quote_impl(self, obs):
        ev = self._estimate_S(obs)
        w = obs.final_cap
        lo = int(round(ev - w / 2.0))
        return (lo, lo + w)

    def _evaluate_substitute_pnl(self, raw_pnl, holds_substitute):
        return max(-2.0, raw_pnl) if holds_substitute else raw_pnl

    def respond(self, obs, quote, turn):
        try:
            return self._respond_impl(obs, quote, turn)
        except Exception:
            bid, ask = quote
            v = obs.k_mine
            if v > ask:
                return "ACCEPT_BUY"
            if v < bid:
                return "ACCEPT_SELL"
            try:
                w = max(0, (ask - bid) - self.config.MIN_REDUCTION)
            except Exception:
                w = max(0, (ask - bid) - 1)
            center = max(bid, min(round(v), ask - w))
            return ("COUNTER", center, center + w)

    def _respond_impl(self, obs, quote, turn):
        bid, ask = quote
        ev = self._estimate_S(obs, quote, turn)
        holds_sub = "SUBSTITUTE" in obs.powers_mine
        is_final = (turn == self.config.N_TURNS)
        net_shift = self._net_shift(obs)

        pnl_buy = self._evaluate_substitute_pnl(ev - ask, holds_sub)
        pnl_sell = self._evaluate_substitute_pnl(bid - ev, holds_sub)

        if is_final:
            if not obs.is_maker:
                midpoint = (bid + ask) // 2
                fill_price = midpoint + net_shift
                raw_force_pnl = (fill_price - ev) - self.config.FORCED_FILL_FEE
                pnl_force = self._evaluate_substitute_pnl(raw_force_pnl, holds_sub)
                if pnl_force > pnl_buy and pnl_force > pnl_sell and pnl_force > -2.0:
                    cur_w = ask - bid
                    new_w = max(obs.final_cap, cur_w - self.config.MIN_REDUCTION)
                    new_bid = int(round(ev - new_w / 2.0))
                    new_ask = new_bid + new_w
                    new_bid = max(bid, min(new_bid, ask - new_w))
                    new_ask = min(ask, new_bid + new_w)
                    return ("COUNTER", int(new_bid), int(new_ask))
            return "ACCEPT_BUY" if pnl_buy >= pnl_sell else "ACCEPT_SELL"

        urgency = (turn / self.config.N_TURNS) * 0.35
        threshold = max(0.15, 0.50 - urgency)

        if pnl_buy >= threshold and pnl_buy >= pnl_sell:
            return "ACCEPT_BUY"
        if pnl_sell >= threshold and pnl_sell > pnl_buy:
            return "ACCEPT_SELL"

        if (ask - bid) <= obs.final_cap:
            if pnl_buy >= 0.0 or pnl_sell >= 0.0:
                return "ACCEPT_BUY" if pnl_buy >= pnl_sell else "ACCEPT_SELL"

        cur_w = ask - bid
        new_w = max(obs.final_cap, cur_w - self.config.MIN_REDUCTION)
        new_bid = int(round(ev - new_w / 2.0))
        new_ask = new_bid + new_w
        if new_ask > ask:
            new_ask = ask
            new_bid = max(bid, new_ask - new_w)
        if new_bid < bid:
            new_bid = bid
            new_ask = min(ask, new_bid + new_w)
        return ("COUNTER", int(new_bid), int(new_ask))

    def use_transform(self, obs):
        try:
            return self._use_transform_impl(obs)
        except Exception:
            return False

    def _use_transform_impl(self, obs):
        opp_k, has_signal = self._opp_k_from_signals(obs)
        if has_signal:
            return abs(opp_k) > abs(obs.k_mine)
        return abs(obs.k_mine) <= FLAT_HAND_THRESHOLD
