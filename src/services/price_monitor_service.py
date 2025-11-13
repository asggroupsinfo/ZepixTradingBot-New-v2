import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from src.models import Trade
from src.config import Config
import logging

class PriceMonitorService:
    """
    Background service to monitor prices every 30 seconds for:
    1. SL hunt re-entry (price reaches SL + offset)
    2. TP continuation re-entry (after TP hit with price gap)
    3. Reversal exit opportunities
    """
    
    def __init__(self, config: Config, mt5_client, reentry_manager, 
                 trend_manager, pip_calculator, trading_engine):
        self.config = config
        self.mt5_client = mt5_client
        self.reentry_manager = reentry_manager
        self.trend_manager = trend_manager
        self.pip_calculator = pip_calculator
        self.trading_engine = trading_engine
        
        self.is_running = False
        self.monitor_task = None
        
        # Track symbols being monitored
        self.monitored_symbols = set()
        
        # SL hunt re-entry tracking
        self.sl_hunt_pending = {}  # symbol -> {'price': sl+offset, 'direction': 'buy', 'chain_id': ...}
        
        # TP re-entry tracking
        self.tp_continuation_pending = {}  # symbol -> {'tp_price': ..., 'direction': ...}
        
        # Exit continuation tracking (Exit Appeared/Reversal signals)
        self.exit_continuation_pending = {}  # symbol -> {'exit_price': ..., 'direction': ..., 'exit_reason': ...}
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)
    
    def get_service_status(self) -> Dict[str, Any]:
        """
        DIAGNOSTIC: Get comprehensive service status for debugging
        Returns detailed information about service state, pending re-entries, and configuration
        """
        return {
            "service_running": self.is_running,
            "monitor_task_active": self.monitor_task is not None and not self.monitor_task.done() if self.monitor_task else False,
            "monitored_symbols": list(self.monitored_symbols),
            "pending_counts": {
                "sl_hunt": len(self.sl_hunt_pending),
                "tp_continuation": len(self.tp_continuation_pending),
                "exit_continuation": len(self.exit_continuation_pending)
            },
            "pending_details": {
                "sl_hunt": dict(self.sl_hunt_pending),
                "tp_continuation": dict(self.tp_continuation_pending),
                "exit_continuation": dict(self.exit_continuation_pending)
            },
            "configuration": {
                "sl_hunt_enabled": self.config["re_entry_config"].get("sl_hunt_reentry_enabled", False),
                "tp_reentry_enabled": self.config["re_entry_config"].get("tp_reentry_enabled", False),
                "exit_continuation_enabled": self.config["re_entry_config"].get("exit_continuation_enabled", False),
                "monitor_interval": self.config["re_entry_config"].get("price_monitor_interval_seconds", 30),
                "sl_hunt_offset_pips": self.config["re_entry_config"].get("sl_hunt_offset_pips", 1.0),
                "tp_continuation_gap_pips": self.config["re_entry_config"].get("tp_continuation_price_gap_pips", 2.0)
            }
        }
    
    def log_service_status(self):
        """DIAGNOSTIC: Log comprehensive service status"""
        status = self.get_service_status()
        self.logger.info(
            f"📊 [SERVICE_STATUS] Price Monitor Service:\n"
            f"  Running: {status['service_running']}\n"
            f"  Task Active: {status['monitor_task_active']}\n"
            f"  Monitored Symbols: {status['monitored_symbols']}\n"
            f"  Pending: SL Hunt={status['pending_counts']['sl_hunt']}, "
            f"TP={status['pending_counts']['tp_continuation']}, "
            f"Exit={status['pending_counts']['exit_continuation']}\n"
            f"  Config: SL Hunt={status['configuration']['sl_hunt_enabled']}, "
            f"TP={status['configuration']['tp_reentry_enabled']}, "
            f"Exit={status['configuration']['exit_continuation_enabled']}"
        )
    
    async def start(self):
        """Start the background price monitoring task"""
        if self.is_running:
            self.logger.warning("Price Monitor Service already running")
            return
        
        try:
            self.is_running = True
            self.monitor_task = asyncio.create_task(self._monitor_loop())
            
            # DIAGNOSTIC: Verify task creation
            if self.monitor_task:
                self.logger.info(
                    f"✅ Price Monitor Service started successfully - "
                    f"Task created: {self.monitor_task}, is_running: {self.is_running}"
                )
            else:
                self.logger.error("❌ Price Monitor Service failed - monitor_task is None")
                
        except Exception as e:
            self.logger.error(f"❌ Error starting Price Monitor Service: {str(e)}")
            import traceback
            traceback.print_exc()
            self.is_running = False
    
    async def stop(self):
        """Stop the background price monitoring task"""
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        self.logger.info("STOPPED: Price Monitor Service stopped")
    
    async def _monitor_loop(self):
        """Main monitoring loop - runs every 30 seconds"""
        interval = self.config["re_entry_config"]["price_monitor_interval_seconds"]
        cycle_count = 0
        
        # DIAGNOSTIC: Log loop start
        self.logger.info(
            f"🔄 Monitor loop started - Interval: {interval}s, "
            f"Config: SL Hunt={self.config['re_entry_config'].get('sl_hunt_reentry_enabled', False)}, "
            f"TP={self.config['re_entry_config'].get('tp_reentry_enabled', False)}, "
            f"Exit={self.config['re_entry_config'].get('exit_continuation_enabled', False)}"
        )
        
        while self.is_running:
            try:
                cycle_count += 1
                cycle_start_time = datetime.now()
                
                # DIAGNOSTIC: Heartbeat logging every 10 cycles (5 minutes)
                if cycle_count % 10 == 0:
                    self.logger.info(
                        f"💓 Monitor loop heartbeat - Cycle #{cycle_count}, "
                        f"Running: {self.is_running}, "
                        f"Pending: SL Hunt={len(self.sl_hunt_pending)}, "
                        f"TP={len(self.tp_continuation_pending)}, "
                        f"Exit={len(self.exit_continuation_pending)}"
                    )
                
                await self._check_all_opportunities()
                
                cycle_duration = (datetime.now() - cycle_start_time).total_seconds()
                if cycle_duration > interval:
                    self.logger.warning(
                        f"⚠️ Monitor cycle took {cycle_duration:.2f}s (longer than interval {interval}s)"
                    )
                
                await asyncio.sleep(interval)
                
            except asyncio.CancelledError:
                self.logger.info("Monitor loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"❌ Monitor loop error: {e}")
                import traceback
                traceback.print_exc()
                await asyncio.sleep(interval)
        
        self.logger.info(f"Monitor loop stopped after {cycle_count} cycles")
    
    async def _check_all_opportunities(self):
        """Check all pending re-entry opportunities"""
        
        # DEBUG: Log monitoring cycle start
        self.logger.debug(
            f"[MONITOR_CYCLE] Checking opportunities - "
            f"SL Hunt: {len(self.sl_hunt_pending)}, "
            f"TP Continuation: {len(self.tp_continuation_pending)}, "
            f"Exit Continuation: {len(self.exit_continuation_pending)}"
        )
        
        # Check SL hunt re-entries
        await self._check_sl_hunt_reentries()
        
        # Check TP continuation re-entries
        await self._check_tp_continuation_reentries()
        
        # Check Exit continuation re-entries (NEW)
        await self._check_exit_continuation_reentries()
        
        # Check Profit Booking chains (NEW)
        await self._check_profit_booking_chains()
    
    async def _check_sl_hunt_reentries(self):
        """
        Check if price has reached SL + offset for automatic re-entry
        After SL hunt, wait for price to recover to SL + 1 pip, then re-enter
        """
        if not self.config["re_entry_config"]["sl_hunt_reentry_enabled"]:
            return
        
        for symbol in list(self.sl_hunt_pending.keys()):
            pending = self.sl_hunt_pending[symbol]
            
            # Get current price from MT5
            current_price = self._get_current_price(symbol, pending['direction'])
            if current_price is None:
                self.logger.debug(f"[SL_HUNT] {symbol}: Failed to get current price")
                continue
            
            target_price = pending['target_price']
            direction = pending['direction']
            chain_id = pending['chain_id']
            sl_price = pending.get('sl_price', 0)
            
            # DEBUG: Log price comparison
            self.logger.debug(
                f"[SL_HUNT] {symbol} {direction.upper()}: "
                f"Current={current_price:.5f} Target={target_price:.5f} "
                f"SL={sl_price:.5f} Gap={abs(current_price - target_price):.5f}"
            )
            
            # Check if price has reached target
            price_reached = False
            if direction == 'buy':
                price_reached = current_price >= target_price
            else:
                price_reached = current_price <= target_price
            
            # DIAGNOSTIC: Detailed price check logging
            price_diff = current_price - target_price if direction == 'buy' else target_price - current_price
            self.logger.info(
                f"🔍 [SL_HUNT_PRICE_CHECK] {symbol} {direction.upper()}: "
                f"Current={current_price:.5f} Target={target_price:.5f} "
                f"SL={sl_price:.5f} Diff={price_diff:.5f} "
                f"Reached={'✅ YES' if price_reached else '❌ NO'}"
            )
            
            if price_reached:
                # Validate trend alignment before re-entry
                logic = pending.get('logic', 'LOGIC1')
                alignment = self.trend_manager.check_logic_alignment(symbol, logic)
                
                # DIAGNOSTIC: Detailed alignment check logging
                self.logger.info(
                    f"🔍 [SL_HUNT_ALIGNMENT] {symbol} {logic}: "
                    f"Aligned={'✅ YES' if alignment['aligned'] else '❌ NO'}, "
                    f"Direction={alignment['direction']}, "
                    f"Details={alignment.get('details', {})}, "
                    f"Failure={alignment.get('failure_reason', 'N/A')}"
                )
                
                if not alignment['aligned']:
                    self.logger.warning(
                        f"⚠️ [SL_HUNT_BLOCKED] {symbol}: Re-entry blocked - "
                        f"Alignment failed: {alignment.get('failure_reason', 'Unknown reason')}"
                    )
                    del self.sl_hunt_pending[symbol]
                    continue
                
                # Check signal direction matches alignment
                signal_direction = "BULLISH" if direction == "buy" else "BEARISH"
                alignment_direction = alignment['direction'].upper()
                if alignment_direction != signal_direction:
                    self.logger.warning(
                        f"⚠️ [SL_HUNT_BLOCKED] {symbol}: Re-entry blocked - "
                        f"Direction mismatch: Signal={signal_direction} != Alignment={alignment_direction}"
                    )
                    del self.sl_hunt_pending[symbol]
                    continue
                
                # Execute SL hunt re-entry
                self.logger.info(f"TRIGGERED: SL Hunt Re-Entry Triggered: {symbol} @ {current_price}")
                
                # Create re-entry order with reduced SL
                await self._execute_sl_hunt_reentry(
                    symbol=symbol,
                    direction=direction,
                    price=current_price,
                    chain_id=chain_id,
                    logic=logic
                )
                
                # Remove from pending
                del self.sl_hunt_pending[symbol]
    
    async def _check_tp_continuation_reentries(self):
        """
        Check if price has moved enough after TP hit for re-entry
        After TP, wait for price gap (e.g., 2 pips), then re-enter with reduced SL
        """
        if not self.config["re_entry_config"]["tp_reentry_enabled"]:
            return
        
        for symbol in list(self.tp_continuation_pending.keys()):
            pending = self.tp_continuation_pending[symbol]
            
            # Get current price from MT5
            current_price = self._get_current_price(symbol, pending['direction'])
            if current_price is None:
                self.logger.debug(f"[TP_CONTINUATION] {symbol}: Failed to get current price")
                continue
            
            tp_price = pending['tp_price']
            direction = pending['direction']
            chain_id = pending['chain_id']
            price_gap_pips = self.config["re_entry_config"]["tp_continuation_price_gap_pips"]
            
            # Calculate pip value for symbol
            symbol_config = self.config["symbol_config"][symbol]
            pip_size = symbol_config["pip_size"]
            price_gap = price_gap_pips * pip_size
            
            # Calculate target price
            if direction == 'buy':
                target_price = tp_price + price_gap
            else:
                target_price = tp_price - price_gap
            
            # DEBUG: Log price comparison
            self.logger.debug(
                f"[TP_CONTINUATION] {symbol} {direction.upper()}: "
                f"Current={current_price:.5f} TP={tp_price:.5f} "
                f"Target={target_price:.5f} Gap={price_gap_pips}pips "
                f"GapPrice={price_gap:.5f}"
            )
            
            # Check if price has moved enough from TP
            gap_reached = False
            if direction == 'buy':
                gap_reached = current_price >= target_price
            else:
                gap_reached = current_price <= target_price
            
            # DIAGNOSTIC: Detailed price gap check logging
            price_diff = current_price - target_price if direction == 'buy' else target_price - current_price
            remaining_gap = abs(current_price - target_price)
            self.logger.info(
                f"🔍 [TP_CONTINUATION_PRICE_CHECK] {symbol} {direction.upper()}: "
                f"Current={current_price:.5f} TP={tp_price:.5f} "
                f"Target={target_price:.5f} Gap={price_gap_pips}pips "
                f"Diff={price_diff:.5f} Remaining={remaining_gap/pip_size:.1f}pips "
                f"Reached={'✅ YES' if gap_reached else '❌ NO'}"
            )
            
            if gap_reached:
                # Validate trend alignment
                logic = pending.get('logic', 'LOGIC1')
                alignment = self.trend_manager.check_logic_alignment(symbol, logic)
                
                # DIAGNOSTIC: Detailed alignment check logging
                self.logger.info(
                    f"🔍 [TP_CONTINUATION_ALIGNMENT] {symbol} {logic}: "
                    f"Aligned={'✅ YES' if alignment['aligned'] else '❌ NO'}, "
                    f"Direction={alignment['direction']}, "
                    f"Details={alignment.get('details', {})}, "
                    f"Failure={alignment.get('failure_reason', 'N/A')}"
                )
                
                if not alignment['aligned']:
                    self.logger.warning(
                        f"⚠️ [TP_CONTINUATION_BLOCKED] {symbol}: Re-entry blocked - "
                        f"Alignment failed: {alignment.get('failure_reason', 'Unknown reason')}"
                    )
                    del self.tp_continuation_pending[symbol]
                    continue
                
                signal_direction = "BULLISH" if direction == "buy" else "BEARISH"
                alignment_direction = alignment['direction'].upper()
                if alignment_direction != signal_direction:
                    self.logger.warning(
                        f"⚠️ [TP_CONTINUATION_BLOCKED] {symbol}: Re-entry blocked - "
                        f"Direction mismatch: Signal={signal_direction} != Alignment={alignment_direction}"
                    )
                    del self.tp_continuation_pending[symbol]
                    continue
                
                # Execute TP continuation re-entry
                self.logger.info(f"TRIGGERED: TP Continuation Re-Entry Triggered: {symbol} @ {current_price}")
                
                await self._execute_tp_continuation_reentry(
                    symbol=symbol,
                    direction=direction,
                    price=current_price,
                    chain_id=chain_id,
                    logic=logic
                )
                
                # Remove from pending
                del self.tp_continuation_pending[symbol]
    
    async def _check_exit_continuation_reentries(self):
        """
        Check for re-entry after Exit Appeared/Reversal exit signals
        After exit (Exit Appeared/Reversal), continue monitoring for re-entry with price gap
        Example: Exit @ 3640.200 → Monitor → Re-entry @ 3642.200 (gap required)
        """
        if not self.config["re_entry_config"].get("exit_continuation_enabled", True):
            return
        
        for symbol in list(self.exit_continuation_pending.keys()):
            pending = self.exit_continuation_pending[symbol]
            
            # Get current price from MT5
            current_price = self._get_current_price(symbol, pending['direction'])
            if current_price is None:
                continue
            
            exit_price = pending['exit_price']
            direction = pending['direction']
            logic = pending.get('logic', 'LOGIC1')
            exit_reason = pending.get('exit_reason', 'EXIT')
            price_gap_pips = self.config["re_entry_config"]["tp_continuation_price_gap_pips"]
            
            # Calculate pip value for symbol
            symbol_config = self.config["symbol_config"][symbol]
            pip_size = symbol_config["pip_size"]
            price_gap = price_gap_pips * pip_size
            
            # Calculate target price
            if direction == 'buy':
                target_price = exit_price + price_gap
            else:
                target_price = exit_price - price_gap
            
            # DEBUG: Log price comparison
            self.logger.debug(
                f"[EXIT_CONTINUATION] {symbol} {direction.upper()} ({exit_reason}): "
                f"Current={current_price:.5f} Exit={exit_price:.5f} "
                f"Target={target_price:.5f} Gap={price_gap_pips}pips "
                f"GapPrice={price_gap:.5f}"
            )
            
            # Check if price has moved enough from exit price (continuation direction)
            gap_reached = False
            if direction == 'buy':
                gap_reached = current_price >= target_price
            else:
                gap_reached = current_price <= target_price
            
            # DIAGNOSTIC: Detailed price gap check logging
            price_diff = current_price - target_price if direction == 'buy' else target_price - current_price
            remaining_gap = abs(current_price - target_price)
            self.logger.info(
                f"🔍 [EXIT_CONTINUATION_PRICE_CHECK] {symbol} {direction.upper()} ({exit_reason}): "
                f"Current={current_price:.5f} Exit={exit_price:.5f} "
                f"Target={target_price:.5f} Gap={price_gap_pips}pips "
                f"Diff={price_diff:.5f} Remaining={remaining_gap/pip_size:.1f}pips "
                f"Reached={'✅ YES' if gap_reached else '❌ NO'}"
            )
            
            if gap_reached:
                # Validate trend alignment (CRITICAL - must match logic)
                alignment = self.trend_manager.check_logic_alignment(symbol, logic)
                
                # DIAGNOSTIC: Detailed alignment check logging
                self.logger.info(
                    f"🔍 [EXIT_CONTINUATION_ALIGNMENT] {symbol} {logic} ({exit_reason}): "
                    f"Aligned={'✅ YES' if alignment['aligned'] else '❌ NO'}, "
                    f"Direction={alignment['direction']}, "
                    f"Details={alignment.get('details', {})}, "
                    f"Failure={alignment.get('failure_reason', 'N/A')}"
                )
                
                if not alignment['aligned']:
                    self.logger.warning(
                        f"⚠️ [EXIT_CONTINUATION_BLOCKED] {symbol} ({exit_reason}): Re-entry blocked - "
                        f"Alignment failed: {alignment.get('failure_reason', 'Unknown reason')}"
                    )
                    del self.exit_continuation_pending[symbol]
                    continue
                
                signal_direction = "BULLISH" if direction == "buy" else "BEARISH"
                alignment_direction = alignment['direction'].upper()
                if alignment_direction != signal_direction:
                    self.logger.warning(
                        f"⚠️ [EXIT_CONTINUATION_BLOCKED] {symbol} ({exit_reason}): Re-entry blocked - "
                        f"Direction mismatch: Signal={signal_direction} != Alignment={alignment_direction}"
                    )
                    del self.exit_continuation_pending[symbol]
                    continue
                
                # Execute Exit continuation re-entry
                self.logger.info(f"TRIGGERED: Exit Continuation Re-Entry Triggered: {symbol} @ {current_price} after {exit_reason}")
                
                # Create new chain for exit continuation
                from src.models import Alert
                entry_signal = Alert(
                    symbol=symbol,
                    tf=pending.get('timeframe', '15M'),
                    signal='buy' if direction == 'buy' else 'sell',
                    type='entry',
                    price=current_price
                )
                
                # Execute via trading engine
                await self.trading_engine.process_alert(entry_signal)
                
                # Remove from pending
                del self.exit_continuation_pending[symbol]
                
                self.logger.info(f"SUCCESS: Exit continuation re-entry executed for {symbol}")
    
    async def _execute_sl_hunt_reentry(self, symbol: str, direction: str, 
                                       price: float, chain_id: str, logic: str):
        """Execute automatic SL hunt re-entry"""
        
        # Get chain info
        chain = self.reentry_manager.active_chains.get(chain_id)
        if not chain or chain.current_level >= chain.max_level:
            return
        
        # Calculate new SL with reduction
        reduction_per_level = self.config["re_entry_config"]["sl_reduction_per_level"]
        sl_adjustment = (1 - reduction_per_level) ** chain.current_level
        
        account_balance = self.mt5_client.get_account_balance()
        lot_size = self.trading_engine.risk_manager.get_fixed_lot_size(account_balance)
        
        # Calculate SL and TP
        sl_price, sl_distance = self.pip_calculator.calculate_sl_price(
            symbol, price, direction, lot_size, account_balance, sl_adjustment
        )
        
        tp_price = self.pip_calculator.calculate_tp_price(
            price, sl_price, direction, self.config["rr_ratio"]
        )
        
        # Create trade
        trade = Trade(
            symbol=symbol,
            entry=price,
            sl=sl_price,
            tp=tp_price,
            lot_size=lot_size,
            direction=direction,
            strategy=logic,
            open_time=datetime.now().isoformat(),
            chain_id=chain_id,
            chain_level=chain.current_level + 1,
            is_re_entry=True
        )
        
        # Place order
        if not self.config["simulate_orders"]:
            trade_id = self.mt5_client.place_order(
                symbol=symbol,
                order_type=direction,
                lot_size=lot_size,
                price=price,
                sl=sl_price,
                tp=tp_price,
                comment=f"{logic}_SL_HUNT_REENTRY"
            )
            if trade_id:
                trade.trade_id = trade_id
        
        # Update chain
        self.reentry_manager.update_chain_level(chain_id, trade.trade_id)
        
        # Add to open trades
        self.trading_engine.open_trades.append(trade)
        self.trading_engine.risk_manager.add_open_trade(trade)
        
        # Send Telegram notification
        sl_reduction_percent = (1 - sl_adjustment) * 100
        self.trading_engine.telegram_bot.send_message(
            f"🔄 SL HUNT RE-ENTRY #{chain.current_level + 1}\n"
            f"Strategy: {logic}\n"
            f"Symbol: {symbol}\n"
            f"Direction: {direction.upper()}\n"
            f"Entry: {price:.5f}\n"
            f"SL: {sl_price:.5f} (-{sl_reduction_percent:.0f}% reduction)\n"
            f"TP: {tp_price:.5f}\n"
            f"Lots: {lot_size:.2f}\n"
            f"Chain: {chain_id}\n"
            f"Level: {chain.current_level + 1}/{chain.max_level}"
        )
    
    async def _execute_tp_continuation_reentry(self, symbol: str, direction: str,
                                               price: float, chain_id: str, logic: str):
        """Execute automatic TP continuation re-entry"""
        
        # Get chain info
        chain = self.reentry_manager.active_chains.get(chain_id)
        if not chain or chain.current_level >= chain.max_level:
            return
        
        # Calculate new SL with reduction
        reduction_per_level = self.config["re_entry_config"]["sl_reduction_per_level"]
        sl_adjustment = (1 - reduction_per_level) ** chain.current_level
        
        account_balance = self.mt5_client.get_account_balance()
        lot_size = self.trading_engine.risk_manager.get_fixed_lot_size(account_balance)
        
        # Calculate SL and TP
        sl_price, sl_distance = self.pip_calculator.calculate_sl_price(
            symbol, price, direction, lot_size, account_balance, sl_adjustment
        )
        
        tp_price = self.pip_calculator.calculate_tp_price(
            price, sl_price, direction, self.config["rr_ratio"]
        )
        
        # Create trade
        trade = Trade(
            symbol=symbol,
            entry=price,
            sl=sl_price,
            tp=tp_price,
            lot_size=lot_size,
            direction=direction,
            strategy=logic,
            open_time=datetime.now().isoformat(),
            chain_id=chain_id,
            chain_level=chain.current_level + 1,
            is_re_entry=True
        )
        
        # Place order
        if not self.config["simulate_orders"]:
            trade_id = self.mt5_client.place_order(
                symbol=symbol,
                order_type=direction,
                lot_size=lot_size,
                price=price,
                sl=sl_price,
                tp=tp_price,
                comment=f"{logic}_TP{chain.current_level}_REENTRY"
            )
            if trade_id:
                trade.trade_id = trade_id
        
        # Update chain
        self.reentry_manager.update_chain_level(chain_id, trade.trade_id)
        
        # Add to open trades
        self.trading_engine.open_trades.append(trade)
        self.trading_engine.risk_manager.add_open_trade(trade)
        
        # Save to database
        tp_level = chain.current_level + 1
        self.trading_engine.db.conn.cursor().execute('''
            INSERT INTO tp_reentry_events VALUES (?,?,?,?,?,?,?,?,?)
        ''', (None, chain_id, symbol, tp_level, chain.total_profit, price, 
              (1-sl_adjustment)*100, 0, datetime.now().isoformat()))
        self.trading_engine.db.conn.commit()
        
        # Send Telegram notification
        sl_reduction_percent = (1 - sl_adjustment) * 100
        self.trading_engine.telegram_bot.send_message(
            f"✅ TP{tp_level} RE-ENTRY\n"
            f"Strategy: {logic}\n"
            f"Symbol: {symbol}\n"
            f"Direction: {direction.upper()}\n"
            f"Entry: {price:.5f}\n"
            f"SL: {sl_price:.5f} (-{sl_reduction_percent:.0f}% reduction)\n"
            f"TP: {tp_price:.5f}\n"
            f"Lots: {lot_size:.2f}\n"
            f"Chain Profit: ${chain.total_profit:.2f}\n"
            f"Level: {tp_level}/{chain.max_level}"
        )
    
    def _get_current_price(self, symbol: str, direction: str) -> Optional[float]:
        """Get current price from MT5 (or simulation)"""
        try:
            if self.config.get("simulate_orders", True):
                # Simulation mode - return None or mock price
                return None
            
            import MetaTrader5 as mt5
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return tick.ask if direction == 'buy' else tick.bid
            return None
        except:
            return None
    
    def register_sl_hunt(self, trade: Trade, logic: str):
        """Register a trade for SL hunt monitoring"""
        
        # DIAGNOSTIC: Verify registration prerequisites
        if not trade.chain_id:
            self.logger.warning(
                f"⚠️ Cannot register SL hunt - Trade {trade.trade_id} has no chain_id"
            )
            return
        
        if not trade.sl or trade.sl == 0:
            self.logger.warning(
                f"⚠️ Cannot register SL hunt - Trade {trade.trade_id} has invalid SL: {trade.sl}"
            )
            return
        
        try:
            symbol_config = self.config["symbol_config"][trade.symbol]
            offset_pips = self.config["re_entry_config"]["sl_hunt_offset_pips"]
            pip_size = symbol_config["pip_size"]
            
            # Calculate target price (SL + offset)
            if trade.direction == 'buy':
                target_price = trade.sl + (offset_pips * pip_size)
            else:
                target_price = trade.sl - (offset_pips * pip_size)
            
            # DIAGNOSTIC: Log registration details
            self.logger.info(
                f"📝 [SL_HUNT_REGISTRATION] Trade {trade.trade_id}: "
                f"Symbol={trade.symbol} Direction={trade.direction} "
                f"SL={trade.sl:.5f} Offset={offset_pips}pips "
                f"Target={target_price:.5f} Chain={trade.chain_id} Logic={logic}"
            )
            
            self.sl_hunt_pending[trade.symbol] = {
                'target_price': target_price,
                'direction': trade.direction,
                'chain_id': trade.chain_id,
                'sl_price': trade.sl,
                'logic': logic
            }
            
            self.monitored_symbols.add(trade.symbol)
            self.logger.info(
                f"✅ REGISTERED: SL Hunt monitoring registered: {trade.symbol} @ {target_price:.5f} "
                f"(Total pending: {len(self.sl_hunt_pending)})"
            )
            
        except KeyError as e:
            self.logger.error(
                f"❌ Error registering SL hunt - Symbol config missing: {trade.symbol}, Error: {str(e)}"
            )
        except Exception as e:
            self.logger.error(f"❌ Error registering SL hunt: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def register_tp_continuation(self, trade: Trade, tp_price: float, logic: str):
        """Register a trade for TP continuation monitoring"""
        
        # DIAGNOSTIC: Verify registration prerequisites
        if not trade.chain_id:
            self.logger.warning(
                f"⚠️ Cannot register TP continuation - Trade {trade.trade_id} has no chain_id"
            )
            return
        
        if not tp_price or tp_price == 0:
            self.logger.warning(
                f"⚠️ Cannot register TP continuation - Invalid TP price: {tp_price}"
            )
            return
        
        try:
            # DIAGNOSTIC: Log registration details
            self.logger.info(
                f"📝 [TP_CONTINUATION_REGISTRATION] Trade {trade.trade_id}: "
                f"Symbol={trade.symbol} Direction={trade.direction} "
                f"TP={tp_price:.5f} Chain={trade.chain_id} Logic={logic}"
            )
            
            self.tp_continuation_pending[trade.symbol] = {
                'tp_price': tp_price,
                'direction': trade.direction,
                'chain_id': trade.chain_id,
                'logic': logic
            }
            
            self.monitored_symbols.add(trade.symbol)
            self.logger.info(
                f"✅ REGISTERED: TP continuation monitoring registered: {trade.symbol} after TP @ {tp_price:.5f} "
                f"(Total pending: {len(self.tp_continuation_pending)})"
            )
            
        except Exception as e:
            self.logger.error(f"❌ Error registering TP continuation: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def stop_tp_continuation(self, symbol: str, reason: str = "Opposite signal received"):
        """Stop TP continuation monitoring for a symbol"""
        if symbol in self.tp_continuation_pending:
            del self.tp_continuation_pending[symbol]
            self.logger.info(f"STOPPED: TP continuation stopped for {symbol}: {reason}")
    
    def register_exit_continuation(self, trade: Trade, exit_price: float, exit_reason: str, logic: str, timeframe: str = '15M'):
        """
        Register continuation monitoring after Exit Appeared/Reversal exit
        Bot will monitor for re-entry with price gap after exit signal
        """
        
        # DIAGNOSTIC: Verify registration prerequisites
        if not exit_price or exit_price == 0:
            self.logger.warning(
                f"⚠️ Cannot register exit continuation - Invalid exit price: {exit_price}"
            )
            return
        
        try:
            # DIAGNOSTIC: Log registration details
            self.logger.info(
                f"📝 [EXIT_CONTINUATION_REGISTRATION] Trade {getattr(trade, 'trade_id', 'N/A')}: "
                f"Symbol={trade.symbol} Direction={trade.direction} "
                f"Exit={exit_price:.5f} Reason={exit_reason} Logic={logic} TF={timeframe}"
            )
            
            self.exit_continuation_pending[trade.symbol] = {
                'exit_price': exit_price,
                'direction': trade.direction,
                'logic': logic,
                'exit_reason': exit_reason,
                'timeframe': timeframe
            }
            
            self.monitored_symbols.add(trade.symbol)
            self.logger.info(
                f"✅ REGISTERED: Exit continuation monitoring registered: {trade.symbol} after {exit_reason} @ {exit_price:.5f} "
                f"(Total pending: {len(self.exit_continuation_pending)})"
            )
            
        except Exception as e:
            self.logger.error(f"❌ Error registering exit continuation: {str(e)}")
            import traceback
            traceback.print_exc()
    
    def stop_exit_continuation(self, symbol: str, reason: str = "Alignment lost"):
        """Stop exit continuation monitoring for a symbol"""
        if symbol in self.exit_continuation_pending:
            del self.exit_continuation_pending[symbol]
            self.logger.info(f"STOPPED: Exit continuation stopped for {symbol}: {reason}")
    
    async def _check_profit_booking_chains(self):
        """
        Check profit booking chains for profit target achievement
        Runs every 30 seconds to monitor combined PnL
        """
        # Check if profit booking enabled
        profit_config = self.config.get("profit_booking_config", {})
        if not profit_config.get("enabled", True):
            return
        
        # Get profit booking manager from trading engine
        profit_manager = getattr(self.trading_engine, 'profit_booking_manager', None)
        if not profit_manager or not profit_manager.is_enabled():
            return
        
        # Periodic cleanup of stale chains (every 5 minutes)
        import time
        if not hasattr(self, '_last_cleanup_time'):
            self._last_cleanup_time = time.time()
        
        if time.time() - self._last_cleanup_time > 300:  # 5 minutes
            profit_manager.cleanup_stale_chains()
            self._last_cleanup_time = time.time()
        
        # Get all active profit chains
        active_chains = profit_manager.get_all_chains()
        if not active_chains:
            return
        
        # Get open trades from trading engine
        open_trades = getattr(self.trading_engine, 'open_trades', [])
        
        # Check each chain
        for chain_id, chain in active_chains.items():
            try:
                # Validate chain state (now with deduplication)
                if not profit_manager.validate_chain_state(chain, open_trades):
                    continue
                
                # Check for orders ready to book (≥ $7 each)
                orders_to_book = profit_manager.check_profit_targets(chain, open_trades)
                
                if orders_to_book:
                    # Book orders individually
                    for order in orders_to_book:
                        success = await profit_manager.book_individual_order(
                            order, chain, open_trades, self.trading_engine
                        )
                        if success:
                            self.logger.info(
                                f"✅ Order {order.trade_id} booked: "
                                f"Chain {chain_id} Level {chain.current_level}"
                            )
                    
                    # Check if all orders in current level are closed - progress to next level
                    await profit_manager.check_and_progress_chain(
                        chain, open_trades, self.trading_engine
                    )
                
            except Exception as e:
                self.logger.error(
                    f"Error checking profit booking chain {chain_id}: {str(e)}"
                )
                import traceback
                traceback.print_exc()
