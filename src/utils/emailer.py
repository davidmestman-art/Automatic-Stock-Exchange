import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

logger = logging.getLogger(__name__)


class TradeEmailer:
    """Send trade notifications via SMTP email.

    Configure via environment variables:
      EMAIL_HOST       — SMTP server hostname (e.g. smtp.gmail.com)
      EMAIL_PORT       — SMTP port (default 587 for TLS)
      EMAIL_USER       — Login address / sender
      EMAIL_PASSWORD   — SMTP password or app password
      NOTIFY_EMAIL     — Recipient address
    """

    def __init__(
        self,
        host: str = "",
        port: int = 587,
        user: str = "",
        password: str = "",
        notify_email: str = "",
    ):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.notify_email = notify_email
        self.active: bool = False  # toggled by user settings

    @classmethod
    def from_env(cls) -> "TradeEmailer":
        return cls(
            host=os.getenv("EMAIL_HOST", ""),
            port=int(os.getenv("EMAIL_PORT", "587")),
            user=os.getenv("EMAIL_USER", ""),
            password=os.getenv("EMAIL_PASSWORD", ""),
            notify_email=os.getenv("NOTIFY_EMAIL", ""),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.notify_email)

    def send_trade(
        self,
        action: str,
        symbol: str,
        shares: float,
        price: float,
        score: float,
        reasons: List[str],
        indicators: Optional[dict] = None,
        pnl: Optional[float] = None,
        pnl_pct: Optional[float] = None,
    ) -> None:
        if not self.active or not self.is_configured:
            return
        try:
            subject, body_text, body_html = self._build_email(
                action, symbol, shares, price, score, reasons,
                indicators or {}, pnl, pnl_pct,
            )
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.user
            msg["To"] = self.notify_email
            msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))
            with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.sendmail(self.user, self.notify_email, msg.as_string())
            logger.info(f"[EMAIL] Sent {action} alert for {symbol}")
        except Exception as e:
            logger.warning(f"[EMAIL] Failed to send trade email: {e}")

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_email(self, action, symbol, shares, price, score, reasons, ind, pnl, pnl_pct):
        rsi        = ind.get("rsi")
        macd       = ind.get("macd_hist")
        ema_fast   = ind.get("ema_fast")
        ema_slow   = ind.get("ema_slow")
        z_score    = ind.get("z_score")
        roc_10     = ind.get("roc_10")
        stoch_rsi  = ind.get("stoch_rsi")
        confidence = ind.get("confidence")

        emoji   = "🟢" if action == "BUY" else "🔴"
        subject = f"{emoji} {action} {symbol} @ ${price:.2f}  |  Score {score:+.3f}"

        # ── Plain text ────────────────────────────────────────────────────────
        lines = [
            f"{action} {symbol}",
            "─" * 40,
            f"Price:         ${price:.2f}",
            f"Quantity:      {shares:.0f} shares",
            f"Signal Score:  {score:+.3f}",
        ]
        if confidence is not None:
            lines.append(f"Confidence:    {confidence:.1%}")
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            lines.append(f"P&L:           {sign}${pnl:.2f}")
            if pnl_pct is not None:
                lines.append(f"P&L %:         {sign}{pnl_pct * 100:.2f}%")
        lines += ["", "Why this trade was made:"]
        for r in reasons:
            lines.append(f"  • {r}")
        lines.append("")
        lines.append("Indicator snapshot:")
        if rsi is not None:
            note = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
            lines.append(f"  RSI:        {rsi:.1f}  ({note})")
        if macd is not None:
            lines.append(f"  MACD Hist:  {macd:+.4f}  ({'bullish' if macd > 0 else 'bearish'})")
        if ema_fast is not None and ema_slow is not None:
            cross = "above slow EMA (bullish)" if ema_fast > ema_slow else "below slow EMA (bearish)"
            lines.append(f"  EMA:        {ema_fast:.2f} / {ema_slow:.2f}  ({cross})")
        if z_score is not None:
            note = "mean reversion opp." if abs(z_score) > 1.5 else "near mean"
            lines.append(f"  Z-Score:    {z_score:+.2f}  ({note})")
        if roc_10 is not None:
            lines.append(f"  ROC-10:     {roc_10 * 100:+.2f}%")
        if stoch_rsi is not None:
            lines.append(f"  StochRSI:   {stoch_rsi:.1f}")
        text = "\n".join(lines)

        # ── HTML ──────────────────────────────────────────────────────────────
        accent = "#10b981" if action == "BUY" else "#ef4444"

        pnl_row = ""
        if pnl is not None:
            sign = "+" if pnl >= 0 else ""
            pc   = "#10b981" if pnl >= 0 else "#ef4444"
            pnl_str = f"{sign}${pnl:.2f}"
            if pnl_pct is not None:
                pnl_str += f" ({sign}{pnl_pct * 100:.2f}%)"
            pnl_row = _tr("P&L", f'<span style="color:{pc};font-weight:700">{pnl_str}</span>')

        conf_row = _tr("Confidence", f"{confidence:.1%}") if confidence is not None else ""

        reasons_html = "".join(
            f'<li style="margin:5px 0;color:#cbd5e1">{r}</li>' for r in reasons
        )

        ind_rows = []
        if rsi is not None:
            note = "oversold" if rsi < 30 else "overbought" if rsi > 70 else "neutral"
            ind_rows.append(_tr("RSI", f'{rsi:.1f} <span style="color:#8898b8;font-size:11px">({note})</span>'))
        if macd is not None:
            d = "bullish" if macd > 0 else "bearish"
            ind_rows.append(_tr("MACD Hist", f'{macd:+.4f} <span style="color:#8898b8;font-size:11px">({d})</span>'))
        if ema_fast is not None and ema_slow is not None:
            cross = "above slow" if ema_fast > ema_slow else "below slow"
            ind_rows.append(_tr("EMA fast/slow", f'{ema_fast:.2f} / {ema_slow:.2f} <span style="color:#8898b8;font-size:11px">({cross})</span>'))
        if z_score is not None:
            ind_rows.append(_tr("Z-Score", f"{z_score:+.2f}"))
        if roc_10 is not None:
            ind_rows.append(_tr("ROC-10", f"{roc_10 * 100:+.2f}%"))
        if stoch_rsi is not None:
            ind_rows.append(_tr("StochRSI", f"{stoch_rsi:.1f}"))
        ind_section = ""
        if ind_rows:
            ind_section = f"""
  <div style="{_card_style}">
    <div style="{_label_style}">INDICATORS</div>
    <table style="width:100%;border-collapse:collapse">
      {''.join(ind_rows)}
    </table>
  </div>"""

        html = f"""<!DOCTYPE html>
<html><body style="background:#07090f;font-family:Inter,'Segoe UI',Arial,sans-serif;color:#eaf0fb;margin:0;padding:32px">
<div style="max-width:520px;margin:0 auto">
  <div style="background:{accent};color:#fff;display:inline-block;padding:5px 16px;border-radius:20px;font-size:12px;font-weight:700;letter-spacing:.6px;margin-bottom:16px">{action}</div>
  <h1 style="font-size:34px;font-weight:800;margin:0 0 4px;letter-spacing:-1px">{symbol}</h1>
  <div style="color:#8898b8;font-size:13px;margin-bottom:28px">NYSE Algo Trading Engine</div>
  <div style="{_card_style}">
    <table style="width:100%;border-collapse:collapse">
      {_tr("Price", f'<span style="font-size:20px;font-weight:800">${price:.2f}</span>')}
      {_tr("Quantity", f"{shares:.0f} shares")}
      {_tr("Signal Score", f'<span style="color:{accent};font-weight:700">{score:+.3f}</span>')}
      {conf_row}
      {pnl_row}
    </table>
  </div>
  <div style="{_card_style}">
    <div style="{_label_style}">WHY THIS TRADE WAS MADE</div>
    <ul style="margin:0;padding-left:18px">{reasons_html}</ul>
  </div>
  {ind_section}
</div>
</body></html>"""
        return subject, text, html


def _tr(label: str, value: str) -> str:
    return (
        f'<tr style="border-bottom:1px solid #1a2540">'
        f'<td style="padding:8px 0;color:#8898b8;font-size:13px">{label}</td>'
        f'<td style="padding:8px 0;color:#eaf0fb;font-size:13px;text-align:right">{value}</td>'
        f'</tr>'
    )


_card_style  = "background:#0d1220;border:1px solid #1a2540;border-radius:12px;padding:20px;margin-bottom:16px"
_label_style = "font-size:11px;font-weight:700;color:#8898b8;letter-spacing:.6px;margin-bottom:12px"
