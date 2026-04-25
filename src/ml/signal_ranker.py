import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed; ML signal ranking disabled")

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "ml_model.pkl"

_FEATURE_KEYS = ["rsi", "macd_hist", "ema_fast", "ema_slow", "score", "confidence"]


class SignalRanker:
    """Learn which indicator combinations produce profitable trades.

    Trains on BUY→SELL pairs extracted from the trade journal and produces
    a score multiplier in [0.8, 1.2] reflecting P(profitable trade).
    """

    MIN_SAMPLES = 20

    def __init__(self, model_path: Path = None, min_samples: int = None):
        self.model_path = model_path or DEFAULT_MODEL_PATH
        self.min_samples = min_samples or self.MIN_SAMPLES
        self.model: Optional[Any] = None
        self._last_trained: Optional[datetime] = None
        self._train_samples: int = 0
        self._val_accuracy: Optional[float] = None
        self._load_model()

    @property
    def is_trained(self) -> bool:
        return self.model is not None

    def maybe_train(self, journal) -> bool:
        """Re-train if new data has arrived since the last training run."""
        if not SKLEARN_AVAILABLE:
            return False
        pairs = self._extract_pairs(journal)
        if len(pairs) < self.min_samples:
            logger.info(
                f"SignalRanker: only {len(pairs)} pairs (need {self.min_samples}), skipping"
            )
            return False
        if self.is_trained and len(pairs) == self._train_samples:
            return False
        return self._train(pairs)

    def score_adjustment(self, ind_dict: dict) -> float:
        """Return a multiplier in [0.8, 1.2] based on P(profitable)."""
        if not self.is_trained or not SKLEARN_AVAILABLE:
            return 1.0
        fv = self._to_feature_vector(ind_dict)
        if fv is None:
            return 1.0
        try:
            classes = list(self.model.classes_)
            pos_idx = classes.index(1) if 1 in classes else -1
            if pos_idx < 0:
                return 1.0
            prob = float(self.model.predict_proba([fv])[0][pos_idx])
            return round(0.8 + prob * 0.4, 4)   # [0,1] → [0.8, 1.2]
        except Exception as e:
            logger.debug(f"SignalRanker.score_adjustment: {e}")
            return 1.0

    def status(self) -> dict:
        return {
            "trained": self.is_trained,
            "samples": self._train_samples,
            "accuracy": self._val_accuracy,
            "last_trained": (
                self._last_trained.isoformat() if self._last_trained else None
            ),
            "sklearn_available": SKLEARN_AVAILABLE,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _extract_pairs(self, journal) -> List[dict]:
        """Match BUY entries (with indicators) to SELL entries (with pnl_pct)."""
        entries = journal.read_all()
        buys: Dict[str, dict] = {}
        pairs: List[dict] = []
        for e in entries:
            action = e.get("action", "")
            symbol = e.get("symbol", "")
            if action == "BUY" and e.get("indicators"):
                buys[symbol] = e
            elif action == "SELL" and symbol in buys:
                buy_entry = buys.pop(symbol)
                pnl_pct = e.get("pnl_pct")
                if pnl_pct is not None:
                    pairs.append({
                        "indicators": buy_entry["indicators"],
                        "profitable": 1 if pnl_pct > 0 else 0,
                    })
        return pairs

    def _to_feature_vector(self, ind: dict) -> Optional[np.ndarray]:
        try:
            rsi = float(ind.get("rsi") or 50.0) / 100.0
            macd_hist = float(ind.get("macd_hist") or 0.0)
            ema_fast = float(ind.get("ema_fast") or 1.0)
            ema_slow = float(ind.get("ema_slow") or 1.0)
            ema_spread = (ema_fast - ema_slow) / max(ema_slow, 1e-8)
            score = float(ind.get("score") or 0.0)
            confidence = float(ind.get("confidence") or 0.0)
            return np.array([rsi, macd_hist, ema_spread, score, confidence])
        except Exception as e:
            logger.debug(f"SignalRanker._to_feature_vector: {e}")
            return None

    def _train(self, pairs: List[dict]) -> bool:
        if not SKLEARN_AVAILABLE:
            return False
        try:
            X, y = [], []
            for p in pairs:
                fv = self._to_feature_vector(p["indicators"])
                if fv is not None:
                    X.append(fv)
                    y.append(p["profitable"])

            if len(X) < self.min_samples:
                return False

            X_arr = np.array(X)
            y_arr = np.array(y)

            split = max(1, int(len(X_arr) * 0.8))
            X_train, X_val = X_arr[:split], X_arr[split:]
            y_train, y_val = y_arr[:split], y_arr[split:]

            pipe = Pipeline([
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(
                    class_weight="balanced",
                    max_iter=500,
                    random_state=42,
                )),
            ])
            pipe.fit(X_train, y_train)

            val_acc: Optional[float] = None
            if len(X_val) > 0:
                val_acc = round(float(pipe.score(X_val, y_val)), 4)

            self.model = pipe
            self._train_samples = len(pairs)
            self._last_trained = datetime.utcnow()
            self._val_accuracy = val_acc

            joblib.dump(pipe, self.model_path)
            logger.info(
                f"SignalRanker: trained on {len(pairs)} pairs"
                + (f", val_acc={val_acc:.2%}" if val_acc is not None else "")
            )
            return True

        except Exception as e:
            logger.error(f"SignalRanker._train: {e}")
            return False

    def _load_model(self) -> None:
        if not SKLEARN_AVAILABLE:
            return
        try:
            if self.model_path.exists():
                import joblib as jl
                self.model = jl.load(self.model_path)
                logger.info(f"SignalRanker: loaded model from {self.model_path}")
        except Exception as e:
            logger.warning(f"SignalRanker: could not load model: {e}")
