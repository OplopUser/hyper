"""
bot/exchange.py — Order placement, cancellation and position management.

All trading actions go through this module.  It wraps the official
hyperliquid-python-sdk's Exchange class and provides:

  - place_market_order()   — market buy/sell with size validation
  - place_limit_order()    — limit buy/sell
  - cancel_order()         — cancel by order id
  - cancel_all_orders()    — cancel every open order for a coin
  - close_position()       — fully close an existing position
  - set_leverage()         — set cross or isolated leverage for a coin
  - get_positions()        — current open perp positions
  - get_account_equity()   — total account value in USD
  - get_open_orders()      — all outstanding orders

The Exchange is initialised lazily so that importing this module in tests
or dry-run mode doesn't require a live private key.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config
from utils.logger import get_logger, log_order

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_size(size: float, decimals: int) -> float:
    """Round order size to the exchange's required decimal precision."""
    return math.floor(size * 10**decimals) / 10**decimals


def _build_exchange() -> Exchange:
    """
    Construct an Exchange instance from config.
    Called once per ExchangeClient instantiation.

    The API wallet (PRIVATE_KEY) signs orders on behalf of the main
    account (ACCOUNT_ADDRESS).  The SDK must be told which main account
    the signer is acting for — otherwise it signs as the API wallet's
    own address and Hyperliquid rejects with
    "User or API Wallet … does not exist".
    """
    private_key = config.get_private_key()
    account_address = config.get_account_address()
    base_url = config.HYPERLIQUID_API_URL

    from eth_account import Account
    account = Account.from_key(private_key)

    exchange = Exchange(
        account,
        base_url=base_url,
        account_address=account_address,
    )
    log.info(
        "Exchange initialised — signer(api_wallet)=%s  owner(account)=%s  network=%s",
        account.address,
        account_address,
        "testnet" if config.USE_TESTNET else "mainnet",
    )
    return exchange


# ---------------------------------------------------------------------------
# ExchangeClient
# ---------------------------------------------------------------------------

class ExchangeClient:
    """
    High-level trading operations built on top of the SDK's Exchange class.
    """

    def __init__(self) -> None:
        self._exchange: Exchange = _build_exchange()
        self._info: Info = Info(
            base_url=config.HYPERLIQUID_API_URL,
            skip_ws=True,
        )
        # Use the MAIN account address for balance/position queries
        # (the API wallet only signs — funds live under the main address)
        self._address: str = config.get_account_address()

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_market_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        sz_decimals: int = 3,
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """
        Place an IOC (immediate-or-cancel) market order.

        Hyperliquid doesn't have a "market" order type directly; the
        conventional approach is to use an aggressive limit order at a price
        far from the market to guarantee fill.  The SDK's order() method with
        orderType={"market": {}} handles this automatically.

        Parameters
        ----------
        coin        : e.g. "BTC"
        is_buy      : True for buy, False for sell
        size        : Order size in base asset units (e.g. 0.001 BTC)
        sz_decimals : Number of decimal places allowed for size
        reduce_only : If True, the order can only reduce an existing position

        Returns the raw SDK response dict, or None on error.
        """
        size = _round_size(size, sz_decimals)
        if size <= 0:
            log.warning("place_market_order: size rounded to 0, skipping.")
            return None

        side_str = "buy" if is_buy else "sell"
        log_order("SUBMIT", coin, side_str, size, None)

        try:
            result = self._exchange.market_open(
                name=coin,
                is_buy=is_buy,
                sz=size,
            )
            self._log_sdk_result(result, coin, side_str, size)
            return result
        except Exception as exc:
            log.error("place_market_order(%s %s %.6f) failed: %s", side_str, coin, size, exc)
            return None

    def place_limit_order(
        self,
        coin: str,
        is_buy: bool,
        size: float,
        price: float,
        sz_decimals: int = 3,
        px_decimals: int = 2,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Optional[dict]:
        """
        Place a resting limit order (Good-Till-Cancel).

        Parameters
        ----------
        price       : Limit price in USD
        post_only   : If True, order is rejected if it would immediately fill
                      (maker-only; uses Alo order type in SDK)
        """
        size  = _round_size(size, sz_decimals)
        price = round(price, px_decimals)
        if size <= 0:
            log.warning("place_limit_order: size rounded to 0, skipping.")
            return None

        side_str = "buy" if is_buy else "sell"
        log_order("LIMIT", coin, side_str, size, price)

        order_type: dict
        if post_only:
            order_type = {"limit": {"tif": "Alo"}}
        else:
            order_type = {"limit": {"tif": "Gtc"}}

        try:
            result = self._exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=size,
                limit_px=price,
                order_type=order_type,
                reduce_only=reduce_only,
            )
            self._log_sdk_result(result, coin, side_str, size, price)
            return result
        except Exception as exc:
            log.error("place_limit_order(%s %s %.6f @ %.4f) failed: %s",
                      side_str, coin, size, price, exc)
            return None

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    def cancel_order(self, coin: str, order_id: int) -> bool:
        """Cancel a single order by its integer order ID. Returns True on success."""
        log.info("Cancelling order_id=%d for %s", order_id, coin)
        try:
            result = self._exchange.cancel(name=coin, oid=order_id)
            ok = self._is_success(result)
            if ok:
                log_order("CANCEL", coin, "n/a", 0, None, str(order_id))
            else:
                log.warning("Cancel order_id=%d returned: %s", order_id, result)
            return ok
        except Exception as exc:
            log.error("cancel_order(%s, %d) failed: %s", coin, order_id, exc)
            return False

    def cancel_all_orders(self, coin: str) -> int:
        """
        Cancel every open order for *coin*.
        Returns the number of successfully cancelled orders.
        """
        open_orders = self.get_open_orders()
        coin_orders = [o for o in open_orders if o.get("coin") == coin]
        if not coin_orders:
            log.debug("No open orders to cancel for %s", coin)
            return 0

        cancelled = 0
        for order in coin_orders:
            oid = order.get("oid")
            if oid and self.cancel_order(coin, oid):
                cancelled += 1
        log.info("Cancelled %d/%d orders for %s", cancelled, len(coin_orders), coin)
        return cancelled

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def close_position(
        self,
        coin: str,
        sz_decimals: int = 3,
    ) -> Optional[dict]:
        """
        Fully close any open position for *coin* with a market order.
        Returns the SDK response or None if there was no position to close.
        """
        position = self.get_position(coin)
        if not position:
            log.info("close_position: no open position for %s", coin)
            return None

        szi = float(position.get("szi", 0))
        if szi == 0:
            return None

        # If szi > 0 we're long → close with a sell; if szi < 0 we're short → buy
        is_buy  = szi < 0
        abs_size = abs(szi)

        log.info(
            "Closing position: %s %s size=%.6f",
            "long" if szi > 0 else "short", coin, abs_size,
        )
        return self.place_market_order(
            coin=coin,
            is_buy=is_buy,
            size=abs_size,
            sz_decimals=sz_decimals,
            reduce_only=True,
        )

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    def set_leverage(
        self,
        coin: str,
        leverage: int,
        is_cross: bool = True,
    ) -> bool:
        """
        Set leverage for *coin*.

        Parameters
        ----------
        leverage : Integer leverage (e.g. 5 for 5×)
        is_cross : True = cross-margin, False = isolated
        """
        try:
            result = self._exchange.update_leverage(
                leverage=leverage,
                name=coin,
                is_cross=is_cross,
            )
            ok = self._is_success(result)
            if ok:
                log.info("Leverage for %s set to %d× (%s)", coin, leverage,
                         "cross" if is_cross else "isolated")
            else:
                log.warning("set_leverage(%s, %d) returned: %s", coin, leverage, result)
            return ok
        except Exception as exc:
            log.error("set_leverage(%s, %d) failed: %s", coin, leverage, exc)
            return False

    # ------------------------------------------------------------------
    # Account / position queries
    # ------------------------------------------------------------------

    def get_user_state(self) -> Optional[dict]:
        """Return the full user-state dict (margins, positions, equity)."""
        try:
            return self._info.user_state(self._address)
        except Exception as exc:
            log.error("get_user_state failed: %s", exc)
            return None

    def get_account_equity(self) -> Optional[float]:
        """
        Return total account equity (margin value) in USD.
        Returns None if the API call fails (so callers can distinguish
        from a genuine zero balance).
        """
        state = self.get_user_state()
        if not state:
            return None
        try:
            return float(state["marginSummary"]["accountValue"])
        except (KeyError, TypeError) as exc:
            log.error("Could not parse accountValue: %s", exc)
            return None

    def get_available_margin(self) -> Optional[float]:
        """
        Return the margin free for a new position (the account's
        ``withdrawable``: equity not already tied up as margin), or None if
        the API call fails.
        """
        state = self.get_user_state()
        if not state:
            return None
        try:
            return float(state["withdrawable"])
        except (KeyError, TypeError, ValueError) as exc:
            log.error("Could not parse withdrawable: %s", exc)
            return None

    def get_positions(self) -> List[dict]:
        """
        Return all open perpetual positions as a list of dicts.

        Each dict contains the raw SDK fields:
          coin, szi (signed size), entryPx, positionValue,
          unrealizedPnl, returnOnEquity, liquidationPx, marginUsed, leverage

        Returns [] both when nothing is open and when the API call fails;
        use get_positions_or_none() when those must be told apart.
        """
        return self.get_positions_or_none() or []

    def get_positions_or_none(self) -> Optional[List[dict]]:
        """Like get_positions(), but returns None if the API call failed."""
        state = self.get_user_state()
        if not state:
            return None
        positions = []
        for asset_pos in state.get("assetPositions", []):
            pos = asset_pos.get("position", {})
            if float(pos.get("szi", 0)) != 0:
                positions.append(pos)
        return positions

    def get_position(self, coin: str) -> Optional[dict]:
        """Return the position dict for *coin*, or None if not open (or the API call failed)."""
        for pos in self.get_positions():
            if pos.get("coin") == coin:
                return pos
        return None

    def get_open_orders(self) -> List[dict]:
        """Return all open orders across all coins."""
        try:
            return self._info.open_orders(self._address)
        except Exception as exc:
            log.error("get_open_orders failed: %s", exc)
            return []

    def get_open_orders_for_coin(self, coin: str) -> List[dict]:
        """Return open orders filtered to a single coin."""
        return [o for o in self.get_open_orders() if o.get("coin") == coin]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_success(result: dict) -> bool:
        """Check whether an SDK response indicates success."""
        if not result:
            return False
        status = result.get("status")
        if status == "ok":
            return True
        # Some responses nest status inside "response"
        inner = result.get("response", {})
        return inner.get("type") == "order" or status == "ok"

    @staticmethod
    def _log_sdk_result(
        result: dict,
        coin: str,
        side: str,
        size: float,
        price: Optional[float] = None,
    ) -> None:
        if not result:
            log.error("SDK returned empty result for %s %s %.6f", side, coin, size)
            log_order("FAILED", coin, side, size, price,
                      extra={"reason": "empty_response"})
            return

        status = result.get("status", "unknown")
        if status == "ok":
            # Extract order ID from the nested response if present
            order_id = None
            try:
                statuses = result["response"]["data"]["statuses"]
                for s in statuses:
                    if "resting" in s:
                        order_id = str(s["resting"]["oid"])
                    elif "filled" in s:
                        order_id = str(s["filled"]["oid"])
            except (KeyError, TypeError):
                pass
            log_order("CONFIRMED", coin, side, size, price, order_id)
        else:
            err_msg = result.get("response", result)
            log.error("Order rejected status=%s: %s", status, err_msg)
            log_order("FAILED", coin, side, size, price,
                      extra={"status": status, "error": str(err_msg)[:160]})
