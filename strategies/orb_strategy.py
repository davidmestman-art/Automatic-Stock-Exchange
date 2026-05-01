git add strategies/orb_strategy.py
git commit -m "Add ORB strategy module"
git push
from strategies.orb_strategy import ORBStrategy

orb = ORBStrategy(opening_minutes=30)

def initialize_orb(prev_high, prev_low, opening_high, opening_low):
    orb.set_previous_day_levels(prev_high, prev_low)
    orb.set_opening_range(opening_high, opening_low)

def get_orb_signal(price):
    return orb.check_breakout(price)
    
