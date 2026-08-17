# Name: Anirban Pal
# College: IIEST Shibpur
# Roll Number: 2025ITB029

"""
OracleStrike v3 (Grandmaster Quant Edition)
===========================================
- Exact Hypergeometric / Uniform FORESIGHT estimator
- Closed-form Expected Payoff Matrix for Negotiation & Force Trap
- Optimal First-Price Auction Sniping against opponent TE state
- Hard-floor Maker Quoting to eliminate 0.22 width penalty
"""

from __future__ import annotations
import math
import random
from typing import Any

# Tick valuations per power per round
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
    name = "OracleStrike_v3"

    def reset(self, seat: int, config: Any, seed: int) -> None:
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        self._opp_k_cache: dict[int, float] = {}
        self._latched: set[int] = set()

    # ─────────────────────────────────────────────────────────────
    # 1. MATHEMATICAL ESTIMATOR (Exact Bayesian Inference)
    # ─────────────────────────────────────────────────────────────
    def _opp_k_from_signals(self, obs: Any, live_quote: tuple[int, int] | None = None, live_turn: int | None = None) -> tuple[float, bool]:
        """Infers the opponent's revealed coin sum from contract history and live quotes."""
        best_round = -1
        best_k = 0.0
        found = False

        # Read past round maker opening quotes
        for c in obs.contracts:
            if c.maker_seat != self.seat:
                mid = (c.open_bid + c.open_ask) / 2.0
                if c.round > best_round:
                    best_round = c.round
                    best_k = mid
                    found = True

        # Latch live quote on Turn 2 if we are Taker (uncontaminated maker read)
        if (live_quote is not None
                and live_turn == 2
                and not obs.is_maker
                and obs.round not in self._latched):
            mid = (live_quote[0] + live_quote[1]) / 2.0
            self._opp_k_cache[obs.round] = mid
            self._latched.add(obs.round)

        for r, k in self._opp_k_cache.items():
            if r > best_round:
                best_round = r
                best_k = k
                found = True

        return (best_k, found)

    def _estimate_S(self, obs: Any, live_quote: tuple[int, int] | None = None, live_turn: int | None = None) -> float:
        """Computes unbiased E[S | Information Set]."""
        my_k = float(obs.k_mine)

        # 1. Perfect / Direct Foresight Math
        if obs.foresight:
            foresight_sum = sum(obs.foresight)
            m = len(obs.foresight)
            total_revealed_opp = 4 * obs.round

            # Direct sample scaling: E[opp_revealed] = (4r / m) * sum(foresight)
            scaled_opp_revealed = (
                float(total_revealed_opp) / float(m)) * float(foresight_sum)
            return my_k + scaled_opp_revealed

        # 2. Quote Inference Math
        opp_k, has_signal = self._opp_k_from_signals(
            obs, live_quote, live_turn)
        if has_signal:
            return my_k + opp_k

        # 3. Prior expectation = 0 for unseen coins
        return my_k

    # ─────────────────────────────────────────────────────────────
    # 2. AUCTION & BUDGET SNIPING
    # ─────────────────────────────────────────────────────────────
    def _net_shift(self, obs: Any) -> int:
        mine = sum(SHIFT_MAGNITUDES.get(p, 0)
                   for p in obs.powers_mine if p in SHIFT_MAGNITUDES)
        theirs = sum(SHIFT_MAGNITUDES.get(p, 0)
                     for p in obs.powers_theirs if p in SHIFT_MAGNITUDES)
        return mine - theirs

    def _get_transform_value(self, obs: Any) -> float:
        base = POWER_FAIR_VALUES.get("TRANSFORM", {}).get(obs.round, 0.0)
        if base <= 0:
            return 0.0

        opp_k, has_signal = self._opp_k_from_signals(obs)
        # If we have a terrible/flat hand, swap is high value
        if obs.k_mine <= -2:
            return base * 1.3
        if abs(obs.k_mine) <= FLAT_HAND_THRESHOLD:
            return base
        # If opponent has a flat hand and ours is great, bid to deny them
        if has_signal and abs(opp_k) <= 2 and obs.k_mine >= 3:
            return base * 0.5
        return 0.0

    def bid(self, obs: Any, offered: list[str]) -> dict[str, int]:
        if not offered or obs.te_mine <= 0:
            return {}

        name = offered[0]
        r = obs.round

        # Fair valuation in ticks
        if name == "TRANSFORM":
            v_ticks = self._get_transform_value(obs)
        else:
            v_ticks = POWER_FAIR_VALUES.get(name, {}).get(r, 0.0)

        if v_ticks <= 0.0:
            return {}

        # 1 tick = 1 / TE_SALVAGE in TE points (12.5 TE per tick)
        fair_te = v_ticks / self.config.TE_SALVAGE
        opp_te = obs.te_theirs

        # Budget reservation: save firepower for r4/r5 Foresight
        rounds_remaining = 6 - r
        reserve = (rounds_remaining - 1) * 3
        max_spendable = max(1, obs.te_mine - reserve)

        # Snipe or shade based on opponent's known balance
        if opp_te == 0:
            bid_te = 1
        elif opp_te < fair_te * 0.65:
            # We can guarantee a win by spending opp_te + 1
            bid_te = min(int(opp_te) + 1, int(fair_te * 0.70))
        else:
            # Standard first-price auction shading (~60-70%)
            shade = 0.55 if r <= 2 else (0.65 if r <= 4 else 0.75)
            bid_te = int(fair_te * shade)

        bid_te = max(0, min(bid_te, max_spendable, obs.te_mine))
        return {name: bid_te} if bid_te > 0 else {}

    # ─────────────────────────────────────────────────────────────
    # 3. QUOTE ENGINE (Floor Width, Zero Width Premium)
    # ─────────────────────────────────────────────────────────────
    def quote(self, obs: Any) -> tuple[int, int]:
        """Opens at the floor width to pay 0.0 in width penalties."""
        ev = self._estimate_S(obs)
        w = obs.final_cap  # Floor width

        # Center quote cleanly around EV
        lo = int(round(ev - w / 2.0))
        return (lo, lo + w)

    # ─────────────────────────────────────────────────────────────
    # 4. NEGOTIATION ENGINE (Payoff Matrix Optimization)
    # ─────────────────────────────────────────────────────────────
    def _evaluate_substitute_pnl(self, raw_pnl: float, holds_substitute: bool) -> float:
        """Substitute caps losses at -2.0 ticks."""
        if holds_substitute:
            return max(-2.0, raw_pnl)
        return raw_pnl

    def respond(self, obs: Any, quote: tuple[int, int], turn: int) -> Any:
        bid, ask = quote
        ev = self._estimate_S(obs, quote, turn)
        holds_sub = "SUBSTITUTE" in obs.powers_mine
        is_final = (turn == self.config.N_TURNS)
        net_shift = self._net_shift(obs)

        # Compute immediate Expected Payoffs
        pnl_buy = self._evaluate_substitute_pnl(ev - ask, holds_sub)
        pnl_sell = self._evaluate_substitute_pnl(bid - ev, holds_sub)

        # ── Turn 6: Terminal Turn Payoff Optimization ────────────
        if is_final:
            if not obs.is_maker:
                # If we counter on turn 6 as Taker:
                # We become SHORT (seller), pay 2.0 forcing fee, fill at midpoint + net_shift
                midpoint = (bid + ask) // 2
                fill_price = midpoint + net_shift
                raw_force_pnl = (fill_price - ev) - self.config.FORCED_FILL_FEE
                pnl_force = self._evaluate_substitute_pnl(
                    raw_force_pnl, holds_sub)

                # Strict utility maximization
                if pnl_force > pnl_buy and pnl_force > pnl_sell and pnl_force > -2.0:
                    # Execute Counter to trigger the trap
                    cur_w = ask - bid
                    new_w = max(obs.final_cap, cur_w -
                                self.config.MIN_REDUCTION)
                    new_bid = int(round(ev - new_w / 2.0))
                    new_ask = new_bid + new_w
                    new_bid = max(bid, min(new_bid, ask - new_w))
                    new_ask = min(ask, new_bid + new_w)
                    return ("COUNTER", int(new_bid), int(new_ask))

            # Otherwise, take the best side to avoid fees
            return "ACCEPT_BUY" if pnl_buy >= pnl_sell else "ACCEPT_SELL"

        # ── Turns 2 to 5: Mid-Game Negotiation ───────────────────
        # Edge threshold: lower it as turns advance to avoid being forced into bad fills
        urgency = (turn / self.config.N_TURNS) * 0.35
        threshold = max(0.15, 0.50 - urgency)

        if pnl_buy >= threshold and pnl_buy >= pnl_sell:
            return "ACCEPT_BUY"
        if pnl_sell >= threshold and pnl_sell > pnl_buy:
            return "ACCEPT_SELL"

        # If already at floor width, no further shrinking is possible
        if (ask - bid) <= obs.final_cap:
            if pnl_buy >= 0.0 or pnl_sell >= 0.0:
                return "ACCEPT_BUY" if pnl_buy >= pnl_sell else "ACCEPT_SELL"

        # Propose optimal counter towards our EV
        cur_w = ask - bid
        new_w = max(obs.final_cap, cur_w - self.config.MIN_REDUCTION)
        new_bid = int(round(ev - new_w / 2.0))
        new_ask = new_bid + new_w

        # Keep clamped strictly inside [bid, ask]
        if new_ask > ask:
            new_ask = ask
            new_bid = max(bid, new_ask - new_w)
        if new_bid < bid:
            new_bid = bid
            new_ask = min(ask, new_bid + new_w)

        return ("COUNTER", int(new_bid), int(new_ask))

    # ─────────────────────────────────────────────────────────────
    # 5. TRANSFORM DECISION
    # ─────────────────────────────────────────────────────────────
    def use_transform(self, obs: Any) -> bool:
        opp_k, has_signal = self._opp_k_from_signals(obs)
        if has_signal:
            return opp_k > obs.k_mine
        return obs.k_mine < 0
