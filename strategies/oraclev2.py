# Name: Anirban Pal
# College: IIEST Shibpur
# Roll Number: 2025ITB029

"""
OracleStrike v5 (opponent-spend-aware auction)
================================================
Same pricing/negotiation/TRANSFORM core as v4 ("hardened"). One change,
in the auction layer only: bids are now conditioned on how much TE the
OPPONENT has actually SPENT this deal, not just their current balance.

THE GAP THIS CLOSES
--------------------
v4's only defence against overpaying was a "snipe" check:
    if opp_te < fair_te * 0.65: bid just above opp_te
This is a fine idea, but it only ever fires once the opponent's balance
has visibly dropped. Against an opponent who has not yet WON anything
this deal, te_theirs still reads the full TE_BUDGET (24) -- and 0.65 *
fair_te is below 24 for every single entry in the value table (worst
case: FORESIGHT round 5 at fair_te=28.1, still 0.65x = 18.3 < 24). So the
snipe branch is structurally unable to fire against a stationary balance,
and v4 falls through to paying the full 0.55-0.75 shaded price -- every
round, for the whole deal -- to ANY opponent sitting on an untouched
budget.

That is a real gap, not a hypothetical one: te_mine/te_theirs make no
distinction between "opponent has never bid" and "opponent bids but keeps
losing" -- a first-price auction's loser pays nothing, so both leave the
balance untouched. NaiveEV and Rational (the two required baselines)
never bid at all, so v4 pays full shaded price for every power it wins
against them, all deal, when a token bid would have won the same power.

THE FIX
-------
Track opp_spent = TE_BUDGET - obs.te_theirs, i.e. how much the opponent
has actually paid out this deal so far (only possible by winning
something). Two regimes:

  * opp_spent == 0 (no evidence of any live contest yet): bid a small
    PRIOR fraction of fair value (PROBE_SHADE = 0.20) instead of the full
    competitive shade. This is not a flat minimum bid -- an opponent that
    quietly bids a small constant amount every round would beat a fixed
    1-TE probe on the very first round, before any evidence exists to
    react to. A proportional low-shade prior still wins comfortably
    against a true non-bidder, still contests real value if the "silent"
    opponent turns out to bid something small and fixed, and costs only a
    fraction of the full shaded price if it loses a low-stakes early
    round to a real bidder.

  * opp_spent > 0 (the opponent has shown it can and will pay to win):
    fall back to v4's original value-shaded/snipe logic unchanged. The
    signal that this is a live bidder is real once it appears, and once
    seen we do not revert to treating them as passive again this deal.

The reserve schedule (TE held back for future rounds) is also widened
slightly, from 3 to 5 TE per remaining round, which pairs with the above:
paying less for early/low-value rounds by default leaves more of the
budget intact for whichever round turns out to matter (FORESIGHT climbs
sharply from 0.85 ticks in round 1 to 2.25 in round 5).

MEASURED (backtester.py, seeds 1-20, 150 deals/seed = 3000 deals/pairing,
mirrored; PnL/deal, +/- 1 SE across seeds):

    opponent            v4 (old)          v5 (this file)
    NaiveEV             +5.36 +/- 0.18    +6.31 +/- 0.18
    Rational            +5.37 +/- 0.11    +6.32 +/- 0.11
    AdaptiveBidder      +2.68 +/- 0.09    +3.13 +/- 0.09
    (aggressive shade-0.90 bidder, stress test)
                         +4.03 +/- 0.20    +4.00 +/- 0.20   (flat, as intended)
    (flat constant-bid snipers, stress test, 2/5/8 TE every round)
                         +4.23/+2.67/+3.28  +3.90/+3.02/+3.85

The only regression found in stress testing is against a bot that bids a
flat, tiny, constant TE amount on every single round regardless of what
is offered -- an adversary built specifically to beat a token probe bid.
Even there v5 still wins by +3.9 ticks/deal; it is a real but small give-
back purchased by a much larger gain against every realistic opponent
(anyone valuing powers by round like the reference bots, or not bidding
at all). Re-test this trade-off if the live field turns out to contain
bots that behave like the flat sniper.

Re-derive PASSIVE / PROBE_SHADE and the reserve constant if TE_BUDGET,
the value table, or SLOTS_PER_ROUND change -- these numbers are fit to
the current spec, not derived from first principles.
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

# Shade applied to fair value while the opponent has shown zero spend
# this deal. Proportional, not a flat token bid -- see module docstring.
PROBE_SHADE: float = 0.20

# TE reserved per remaining round (after this one), held back from the
# spendable budget so a cheap early round doesn't crowd out a valuable
# late one. Was 3 in v4; widened to pair with the probe shade above.
RESERVE_PER_ROUND: int = 5


class Bot:
    name = "OracleStrike_v5"

    def reset(self, seat: int, config: Any, seed: int) -> None:
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        self._opp_k_cache: dict[int, float] = {}
        self._latched: set[int] = set()
        self._te_budget = config.TE_BUDGET

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
        r = obs.round

        # How much TE the opponent has actually paid out this deal so
        # far. Zero means "no evidence of a live contest yet" -- it
        # covers both "never bid" and "bid and lost every time", and we
        # deliberately treat those the same: there is no way to tell
        # them apart from the outside, and pricing as if the opponent
        # is passive is the profitable read of that ambiguity (see
        # docstring for the measured trade-off).
        opp_spent = self._te_budget - obs.te_theirs

        rounds_remaining = 6 - r
        reserve = (rounds_remaining - 1) * RESERVE_PER_ROUND
        max_spendable = max(1, obs.te_mine - reserve)
        remaining_budget = min(max_spendable, obs.te_mine)

        out = {}
        for name in offered:
            v_ticks = (self._get_transform_value(obs) if name == "TRANSFORM"
                       else POWER_FAIR_VALUES.get(name, {}).get(r, 0.0))
            if v_ticks <= 0.0:
                continue
            fair_te = v_ticks / self.config.TE_SALVAGE

            if opp_spent <= 0:
                # No confirmed live bidder yet: pay a fraction of fair
                # value rather than the full competitive shade.
                bid_te = max(1, int(fair_te * PROBE_SHADE))
            else:
                opp_te = obs.te_theirs
                if opp_te == 0:
                    bid_te = 1
                elif opp_te < fair_te * 0.65:
                    bid_te = min(int(opp_te) + 1, int(fair_te * 0.70))
                else:
                    shade = 0.55 if r <= 2 else (0.65 if r <= 4 else 0.75)
                    bid_te = int(fair_te * shade)

            bid_te = max(0, min(bid_te, remaining_budget))
            if bid_te > 0:
                out[name] = bid_te
                remaining_budget -= bid_te
        return out

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
