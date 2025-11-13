from typing import Tuple
from src.config import Config

class ProfitBookingSLCalculator:
    """
    Independent SL calculator for profit booking orders only
    Calculates SL that gives exactly $10 loss per order
    This is separate from TP Trail's SL system
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.fixed_sl_dollar = 10.0  # $10 fixed SL per profit booking order
    
    def calculate_sl_price(self, entry_price: float, direction: str, 
                          symbol: str, lot_size: float) -> Tuple[float, float]:
        """
        Calculate SL price for exactly $10 loss per profit booking order
        Returns: (sl_price, sl_distance_in_price)
        
        Args:
            entry_price: Entry price of the order
            direction: 'buy' or 'sell'
            symbol: Trading symbol (e.g., 'XAUUSD')
            lot_size: Lot size of the order
        """
        try:
            # Get symbol configuration
            symbol_config = self.config["symbol_config"][symbol]
            pip_size = symbol_config["pip_size"]
            pip_value_per_std_lot = symbol_config["pip_value_per_std_lot"]
            
            # Calculate pip value for this specific lot size
            pip_value = pip_value_per_std_lot * lot_size
            
            # Calculate how many pips needed for $10 loss
            # If pip_value is $1 per pip, we need 10 pips for $10 loss
            # If pip_value is $0.1 per pip, we need 100 pips for $10 loss
            sl_pips = self.fixed_sl_dollar / pip_value
            
            # Convert pips to price distance
            sl_distance = sl_pips * pip_size
            
            # Calculate actual SL price based on direction
            if direction == "buy":
                sl_price = entry_price - sl_distance
            else:  # sell
                sl_price = entry_price + sl_distance
            
            return sl_price, sl_distance
            
        except KeyError as e:
            # Fallback if symbol not found in config
            print(f"WARNING: Symbol {symbol} not found in config, using fallback SL calculation")
            # Use a conservative fallback: 50 pips for $10 loss
            pip_size = 0.01  # Default pip size
            pip_value_per_std_lot = 10.0  # Default pip value
            pip_value = pip_value_per_std_lot * lot_size
            sl_pips = self.fixed_sl_dollar / pip_value
            sl_distance = sl_pips * pip_size
            
            if direction == "buy":
                sl_price = entry_price - sl_distance
            else:
                sl_price = entry_price + sl_distance
            
            return sl_price, sl_distance
        except Exception as e:
            print(f"ERROR: Error calculating profit booking SL: {str(e)}")
            # Return a safe fallback
            if direction == "buy":
                return entry_price - (0.01 * 50), 0.01 * 50
            else:
                return entry_price + (0.01 * 50), 0.01 * 50
    
    def get_pip_value(self, symbol: str, lot_size: float) -> float:
        """
        Get pip value for a specific symbol and lot size
        Returns pip value in dollars
        """
        try:
            symbol_config = self.config["symbol_config"][symbol]
            pip_value_per_std_lot = symbol_config["pip_value_per_std_lot"]
            return pip_value_per_std_lot * lot_size
        except KeyError:
            # Fallback
            return 10.0 * lot_size
    
    def validate_sl_loss(self, entry_price: float, sl_price: float, 
                        direction: str, symbol: str, lot_size: float) -> dict:
        """
        Validate that SL will result in exactly $10 loss (within tolerance)
        Returns: {"valid": bool, "actual_loss": float, "expected_loss": float, "difference": float}
        """
        try:
            # Calculate actual loss
            pip_size = self.config["symbol_config"][symbol]["pip_size"]
            pip_value_per_std_lot = self.config["symbol_config"][symbol]["pip_value_per_std_lot"]
            pip_value = pip_value_per_std_lot * lot_size
            
            # Calculate price difference in pips
            price_diff = abs(entry_price - sl_price)
            pips = price_diff / pip_size
            
            # Calculate actual loss
            actual_loss = pips * pip_value
            
            # Check if within 5% tolerance
            expected_loss = self.fixed_sl_dollar
            tolerance = expected_loss * 0.05  # 5% tolerance
            difference = abs(actual_loss - expected_loss)
            
            is_valid = difference <= tolerance
            
            return {
                "valid": is_valid,
                "actual_loss": actual_loss,
                "expected_loss": expected_loss,
                "difference": difference,
                "tolerance": tolerance
            }
        except Exception as e:
            return {
                "valid": False,
                "actual_loss": 0.0,
                "expected_loss": self.fixed_sl_dollar,
                "difference": self.fixed_sl_dollar,
                "error": str(e)
            }

