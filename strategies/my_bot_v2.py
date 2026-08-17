# Name: Anirban Pal
# College: IIEST Shibpur
# Roll Number: 2025ITB029

# SCRATCH FILE: Version 2 candidate, NOT a submission.
# Diff vs my_bot.py (Version 3):
#   - at_floor accept branch added in respond()
#   - is_last_before_final shift-edge guard REPLACED with continuous
#     "eagerness" term: threshold -= min(shift_edge, 3.0) * (turn / N_TURNS)
#   - long FIX-comment block removed
# Verify with Sonnet 5 before swapping into my_bot.py.

import random

POWER_VALUES = {
    "FORESIGHT":    {1: 0.76, 2: 1.16, 3: 1.48, 4: 1.97, 5: 2.02},
    "TRICK_ROOM":   {1: 1.14, 2: 0.00, 3: 0.00, 4: 0.60, 5: 0.52},
    "SUBSTITUTE":   {1: 1.46, 2: 1.15, 3: 0.95, 4: 0.57, 5: 0.29},
    "STEALTH_ROCK": {1: 1.51, 2: 0.75, 3: 0.75, 4: 0.75, 5: 0.00},
    "TRANSFORM":    {1: 1.58, 2: 1.24, 3: 1.31, 4: 0.00, 5: 0.00},
}

SHIFT_MAGNITUDES = {"TRICK_ROOM": 3, "STEALTH_ROCK": 2}
FLAT_THRESHOLD = 2

SHADE_BY_ROUND = {1: 0.55, 2: 0.55, 3: 0.60, 4: 0.70, 5: 0.72}
MAX_FRACTION_BY_ROUND = {1: 0.45, 2: 0.55, 3: 0.65, 4: 0.85, 5: 1.00}


class Bot:
    name = "OracleStrike"

    def reset(self, seat, config, seed):
        self.seat = seat
        self.config = config
        self.rng = random.Random(seed)
        self._opp_k_cache = {}
        self._latched = set()

    def _opp_k_from_contracts(self, obs, live_quote=None, live_turn=None):
        best_round = -1
        best_k = 0.0
        for c in obs.contracts:
            if c.maker_seat != self.seat:
                mid = (c.open_bid + c.open_ask) / 2.0
                if c.round > best_round:
                    best_round = c.round
                    best_k = mid
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
        return best_k

    def _opp_signal(self, obs):
        has_signal = any(c.maker_seat != self.seat for c in obs.contracts)
        if not has_signal:
            return 0.0, False
        return self._opp_k_from_contracts(obs), True

    def _estimate_S(self, obs, live_quote=None, live_turn=None):
        if obs.foresight:
            foresight_sum = sum(obs.foresight)
            total_opp_revealed = 4 * obs.round
            foresight_count = len(obs.foresight)
            unseen_count = total_opp_revealed - foresight_count
            if unseen_count <= 0:
                return float(obs.k_mine) + foresight_sum
            contract_est = self._opp_k_from_contracts(obs, live_quote, live_turn)
            unseen_fraction = unseen_count / total_opp_revealed
            opp_k = foresight_sum + contract_est * unseen_fraction
            return float(obs.k_mine) + opp_k
        opp_k = self._opp_k_from_contracts(obs, live_quote, live_turn)
        return float(obs.k_mine) + opp_k

    def _net_shift(self, obs):
        mine = sum(SHIFT_MAGNITUDES.get(p, 0)
                   for p in obs.powers_mine if p in SHIFT_MAGNITUDES)
        theirs = sum(SHIFT_MAGNITUDES.get(p, 0)
                     for p in obs.powers_theirs if p in SHIFT_MAGNITUDES)
        return mine - theirs

    def _should_force(self, obs):
        return self._net_shift(obs) > self.config.FORCED_FILL_FEE

    def _opp_shift_edge(self, obs):
        return max(0.0, -self._net_shift(obs) - self.config.FORCED_FILL_FEE)

    def _power_value(self, obs, name):
        if name == "TRANSFORM":
            return self._transform_value(obs)
        return POWER_VALUES.get(name, {}).get(obs.round, 0.0)

    def _transform_value(self, obs):
        base = POWER_VALUES.get("TRANSFORM", {}).get(obs.round, 0.0)
        if base <= 0:
            return 0.0
        opp_k, has_signal = self._opp_signal(obs)
        if has_signal and abs(opp_k) > abs(obs.k_mine):
            return base
        if abs(obs.k_mine) <= FLAT_THRESHOLD:
            return base
        if has_signal and abs(opp_k) <= 2.0:
            return base * 0.4
        return 0.0

    def bid(self, obs, offered):
        if not offered or obs.te_mine <= 0:
            return {}
        r = obs.round
        name = offered[0]
        v = self._power_value(obs, name)
        if v <= 0:
            return {}
        fair_te = v / self.config.TE_SALVAGE
        opp_te = obs.te_theirs
        shade = SHADE_BY_ROUND.get(r, 0.60)
        max_frac = MAX_FRACTION_BY_ROUND.get(r, 1.0)
        budget_cap = int(obs.te_mine * max_frac)
        if opp_te == 0:
            bid_te = 1
        elif opp_te < fair_te * shade * 0.6:
            bid_te = min(int(opp_te) + 1, int(fair_te * (shade + 0.05)))
        else:
            bid_te = int(fair_te * shade)
        bid_te = max(0, min(bid_te, budget_cap, obs.te_mine))
        return {name: bid_te} if bid_te > 0 else {}

    def quote(self, obs):
        v = round(float(obs.k_mine) + sum(obs.foresight))
        w = obs.final_cap
        lo = v - w // 2
        return (lo, lo + w)

    def respond(self, obs, quote, turn):
        bid, ask = quote
        v = self._estimate_S(obs, quote, turn)
        edge_buy = v - ask
        edge_sell = bid - v

        sub_bonus = 1.0 if "SUBSTITUTE" in obs.powers_mine else 0.0

        # Continuous "eagerness": closer to the final turn, the more
        # willing we are to accept rather than let a forced fill favour
        # the opponent's shift power.
        shift_edge = self._opp_shift_edge(obs)
        turn_factor = turn / self.config.N_TURNS
        eagerness = min(shift_edge, 3.0) * turn_factor

        threshold = 0.5 - sub_bonus - eagerness
        is_final = (turn == self.config.N_TURNS)
        at_floor = (ask - bid) <= obs.final_cap

        # Countering at floor width does nothing (width can't shrink
        # below the floor), so if we have any edge in the right direction
        # we should just accept.
        if at_floor and not is_final:
            floor_bar = -sub_bonus - eagerness
            if edge_buy >= edge_sell and edge_buy > floor_bar:
                return "ACCEPT_BUY"
            elif edge_sell > edge_buy and edge_sell > floor_bar:
                return "ACCEPT_SELL"

        if is_final:
            if not obs.is_maker and self._should_force(obs):
                cur_w = ask - bid
                new_w = max(obs.final_cap, cur_w - self.config.MIN_REDUCTION)
                new_bid = round(v) - new_w // 2
                new_ask = new_bid + new_w
                if new_ask > ask:
                    new_ask = ask
                    new_bid = new_ask - new_w
                if new_bid < bid:
                    new_bid = bid
                    new_ask = new_bid + new_w
                return ("COUNTER", new_bid, new_ask)
            if edge_buy >= edge_sell:
                return "ACCEPT_BUY"
            else:
                return "ACCEPT_SELL"

        if edge_buy > threshold and edge_buy >= edge_sell:
            return "ACCEPT_BUY"
        if edge_sell > threshold:
            return "ACCEPT_SELL"

        cur_w = ask - bid
        new_w = max(obs.final_cap, cur_w - self.config.MIN_REDUCTION)
        new_bid = round(v) - new_w // 2
        new_ask = new_bid + new_w
        if new_ask > ask:
            new_ask = ask
            new_bid = new_ask - new_w
        if new_bid < bid:
            new_bid = bid
            new_ask = new_bid + new_w
        return ("COUNTER", new_bid, new_ask)

    def use_transform(self, obs):
        opp_k, has_signal = self._opp_signal(obs)
        if has_signal and abs(opp_k) > abs(obs.k_mine):
            return True
        return abs(obs.k_mine) <= FLAT_THRESHOLD
