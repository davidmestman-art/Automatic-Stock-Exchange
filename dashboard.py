#!/usr/bin/env python3
"""Web dashboard for the NYSE Algorithmic Trading Engine.

Run:  python dashboard.py
Then open http://localhost:8080
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

from flask import (
    Flask, jsonify, make_response, redirect, render_template_string,
    request, session, url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from config import TradingConfig, config
from src.data.extended_hours import ExtendedHoursMonitor
from src.trading.engine import TradingEngine
from src.utils.journal import TradeJournal
from src.utils.models import User, db
from src.utils.sectors import get_sector, positions_by_sector

CYCLE_INTERVAL = 60  # seconds between automatic trading cycles

# Configure logging at module level so it works under gunicorn too
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    stream=__import__("sys").stdout,
    force=True,
)

app = Flask(__name__)
# Tell Flask it's behind Railway's HTTPS reverse proxy so it reads
# X-Forwarded-Proto/Host correctly — required for secure session cookies.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── Database ──────────────────────────────────────────────────────────────────
_DB_PATH = Path(__file__).resolve().parent / "users.db"
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db.init_app(app)

with app.app_context():
    db.create_all()
    # Migrate: add Alpaca columns to existing tables created before this column existed
    from sqlalchemy import text as _sql
    with db.engine.connect() as _conn:
        for _col in [
            "ALTER TABLE users ADD COLUMN alpaca_api_key_enc    TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN alpaca_secret_key_enc TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE users ADD COLUMN alpaca_paper          INTEGER NOT NULL DEFAULT 1",
        ]:
            try:
                _conn.execute(_sql(_col))
                _conn.commit()
            except Exception:
                pass  # column already exists

# ── Encryption helpers (Fernet key derived from app secret) ───────────────────
def _make_fernet() -> Fernet:
    raw = hashlib.sha256(
        (app.secret_key if isinstance(app.secret_key, str)
         else app.secret_key.decode("utf-8", errors="replace"))
        .encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _encrypt_key(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _make_fernet().encrypt(plaintext.encode()).decode()


def _decrypt_key(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _make_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return ""


# ── Public landing page ───────────────────────────────────────────────────────
_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>NYSE Trading Engine — Algorithmic Trading for Everyone</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090f;--surface:#0d1220;--surface2:#121a2e;
  --border:#1a2540;--border2:#223060;
  --accent:#2563eb;--accent2:#3b82f6;
  --green:#10b981;--green2:#34d399;
  --text:#eaf0fb;--text2:#8898b8;--text3:#4a5a78;
}
body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
/* Nav */
nav{position:sticky;top:0;z-index:50;display:flex;align-items:center;justify-content:space-between;
    padding:0 48px;height:64px;background:rgba(8,12,20,.92);
    border-bottom:1px solid rgba(30,45,69,.7);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}
.nav-brand{display:flex;align-items:center;gap:10px}
.nav-dot{width:9px;height:9px;background:var(--accent2);border-radius:50%;box-shadow:0 0 12px var(--accent2);flex-shrink:0}
.nav-name{font-size:16px;font-weight:700;color:#f1f5f9;letter-spacing:-.4px}
.nav-actions{display:flex;align-items:center;gap:10px}
.btn-outline{padding:8px 20px;border-radius:7px;border:1px solid var(--border);background:transparent;
             color:var(--text2);font-size:13px;font-weight:600;text-decoration:none;
             display:inline-block;transition:all .15s}
.btn-outline:hover{border-color:var(--accent2);color:var(--text)}
.btn-solid{padding:8px 22px;border-radius:7px;background:var(--accent);color:#fff;font-size:13px;
           font-weight:700;text-decoration:none;display:inline-block;transition:all .15s;
           box-shadow:0 0 20px rgba(37,99,235,.3)}
.btn-solid:hover{background:var(--accent2);box-shadow:0 0 28px rgba(59,130,246,.45)}
/* Hero */
.hero{position:relative;padding:120px 48px 100px;text-align:center;overflow:hidden}
.hero::before{content:'';position:absolute;inset:0;pointer-events:none;
  background:radial-gradient(ellipse 80% 50% at 50% -10%,rgba(37,99,235,.18) 0%,transparent 70%),
             radial-gradient(ellipse 40% 30% at 80% 80%,rgba(16,185,129,.06) 0%,transparent 60%)}
.hero-eyebrow{display:inline-flex;align-items:center;gap:7px;padding:5px 14px;border-radius:99px;
              background:rgba(37,99,235,.12);border:1px solid rgba(37,99,235,.25);
              font-size:12px;font-weight:600;color:var(--accent2);letter-spacing:.5px;
              text-transform:uppercase;margin-bottom:28px}
.eyebrow-dot{width:6px;height:6px;background:var(--accent2);border-radius:50%;box-shadow:0 0 8px var(--accent2)}
.hero h1{font-size:clamp(42px,6vw,72px);font-weight:800;letter-spacing:-2.5px;line-height:1.06;
         margin-bottom:24px;color:#f8fafc}
.grad{background:linear-gradient(135deg,#60a5fa 0%,#a78bfa 50%,#34d399 100%);
      -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{font-size:clamp(16px,2vw,20px);color:var(--text2);max-width:580px;margin:0 auto 48px;line-height:1.65}
.hero-cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:56px}
.btn-hero-p{padding:14px 36px;border-radius:8px;background:var(--accent);color:#fff;
            font-size:16px;font-weight:700;letter-spacing:-.2px;text-decoration:none;
            display:inline-block;box-shadow:0 0 40px rgba(37,99,235,.45);transition:all .2s}
.btn-hero-p:hover{background:var(--accent2);box-shadow:0 0 60px rgba(59,130,246,.55);transform:translateY(-1px)}
.btn-hero-s{padding:13px 32px;border-radius:8px;background:transparent;color:var(--text);
            font-size:16px;font-weight:600;text-decoration:none;display:inline-block;
            border:1px solid var(--border);transition:all .2s}
.btn-hero-s:hover{border-color:var(--accent2);background:rgba(37,99,235,.06);transform:translateY(-1px)}
.hero-chips{display:flex;justify-content:center;gap:16px;flex-wrap:wrap}
.chip{display:flex;align-items:center;gap:7px;padding:8px 16px;border-radius:8px;
      background:rgba(15,22,41,.7);border:1px solid var(--border);
      font-size:13px;font-weight:600;color:var(--text2);backdrop-filter:blur(8px)}
.chip .val{color:var(--text);font-size:14px}
/* Features */
.section{padding:80px 48px;max-width:1200px;margin:0 auto}
.eyebrow{font-size:12px;font-weight:700;color:var(--accent2);text-transform:uppercase;
         letter-spacing:.7px;text-align:center;margin-bottom:12px}
.sec-title{font-size:clamp(28px,4vw,40px);font-weight:800;letter-spacing:-1px;text-align:center;
           margin-bottom:10px;color:#f1f5f9}
.sec-sub{text-align:center;color:var(--text2);font-size:16px;max-width:520px;margin:0 auto 52px}
/* Stats bar below hero CTA */
.stats-bar{display:inline-flex;align-items:center;gap:0;border:1px solid var(--border);
           border-radius:12px;overflow:hidden;background:rgba(13,18,32,.7);
           backdrop-filter:blur(12px);margin-top:20px}
.stat-item{display:flex;flex-direction:column;align-items:center;padding:14px 28px;gap:2px}
.stat-val{font-size:18px;font-weight:800;color:#f1f5f9;letter-spacing:-.5px;line-height:1}
.stat-sep{font-size:11px;color:var(--text3);font-weight:500;text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.stat-divider{width:1px;height:40px;background:var(--border);flex-shrink:0}
/* Feature grid — 2×2 */
.section-wrap{border-top:1px solid var(--border)}
.feat-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px}
.feat-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:28px;
           transition:all .25s;position:relative;overflow:hidden}
.feat-card::after{content:'';position:absolute;top:0;left:0;right:0;height:1px;
                  background:linear-gradient(90deg,transparent,rgba(59,130,246,.4),transparent);
                  opacity:0;transition:opacity .25s}
.feat-card:hover{border-color:rgba(59,130,246,.35);transform:translateY(-3px);box-shadow:0 16px 48px rgba(0,0,0,.5)}
.feat-card:hover::after{opacity:1}
.feat-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;
           justify-content:center;font-size:24px;margin-bottom:18px}
.fi-blue{background:rgba(37,99,235,.15);border:1px solid rgba(37,99,235,.2)}
.fi-green{background:rgba(16,185,129,.12);border:1px solid rgba(16,185,129,.18)}
.fi-purple{background:rgba(139,92,246,.12);border:1px solid rgba(139,92,246,.18)}
.fi-cyan{background:rgba(6,182,212,.1);border:1px solid rgba(6,182,212,.15)}
.fi-amber{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.15)}
.fi-rose{background:rgba(244,63,94,.1);border:1px solid rgba(244,63,94,.15)}
.feat-title{font-size:17px;font-weight:700;color:#f1f5f9;margin-bottom:10px;letter-spacing:-.3px}
.feat-desc{font-size:14px;color:var(--text2);line-height:1.65}
/* How it works */
.hiw{padding:80px 48px;background:linear-gradient(180deg,var(--bg) 0%,var(--surface) 30%,var(--surface) 70%,var(--bg) 100%);
     border-top:1px solid var(--border);border-bottom:1px solid var(--border)}
.hiw-inner{max-width:1000px;margin:0 auto}
.hiw-steps{display:grid;grid-template-columns:repeat(3,1fr);gap:48px;position:relative;margin-top:48px}
.hiw-steps::before{content:'';position:absolute;top:27px;left:calc(16.666% + 16px);
                   right:calc(16.666% + 16px);height:1px;
                   background:linear-gradient(90deg,var(--border),var(--accent2),var(--border));z-index:0}
.hiw-step{text-align:center;position:relative;z-index:1}
.hiw-num{width:56px;height:56px;border-radius:50%;background:var(--bg);border:2px solid var(--accent2);
         display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:800;
         color:var(--accent2);margin:0 auto 20px;box-shadow:0 0 28px rgba(59,130,246,.2)}
.hiw-title{font-size:17px;font-weight:700;color:#f1f5f9;margin-bottom:8px;letter-spacing:-.3px}
.hiw-desc{font-size:14px;color:var(--text2);line-height:1.65}
/* Stats */
.proof{padding:80px 48px;max-width:1200px;margin:0 auto}
.proof-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}
.proof-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
            padding:28px;text-align:center;transition:border-color .2s}
.proof-card:hover{border-color:var(--border2)}
.proof-val{font-size:36px;font-weight:800;letter-spacing:-1.5px;margin-bottom:6px;line-height:1}
.pv-blue{color:var(--accent2)}.pv-green{color:#34d399}.pv-purple{color:#a78bfa}.pv-amber{color:#fbbf24}
.proof-lbl{font-size:13px;color:var(--text2);font-weight:500;line-height:1.4}
/* Footer */
footer{border-top:1px solid var(--border);padding:32px 48px;display:flex;align-items:center;
       justify-content:space-between;flex-wrap:wrap;gap:16px}
.footer-brand{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;color:#f1f5f9}
.footer-dot{width:7px;height:7px;background:var(--accent2);border-radius:50%;flex-shrink:0}
.footer-links{display:flex;gap:24px}
.footer-link{font-size:13px;color:var(--text3);transition:color .15s}
.footer-link:hover{color:var(--text2)}
.footer-note{font-size:12px;color:var(--text3);max-width:360px;text-align:right}
/* Responsive */
@media(max-width:900px){
  nav{padding:0 24px}
  .hero{padding:80px 24px 64px}
  .section,.proof{padding:60px 24px}
  .hiw{padding:60px 24px}
  .feat-grid{grid-template-columns:1fr 1fr}
  .proof-grid{grid-template-columns:1fr 1fr}
  .hiw-steps{grid-template-columns:1fr;gap:28px}
  .hiw-steps::before{display:none}
  footer{padding:24px}
  .footer-note{text-align:left}
}
@media(max-width:600px){
  nav{padding:0 16px;height:56px}
  .nav-name{font-size:14px}
  .hero{padding:60px 16px 48px}
  .section,.proof{padding:48px 16px}
  .hiw{padding:48px 16px}
  .feat-grid{grid-template-columns:1fr}
  .proof-grid{grid-template-columns:1fr 1fr}
  .stats-bar{flex-wrap:wrap;border-radius:10px}
  .stat-item{padding:10px 18px}
  .stat-divider{display:none}
  .stat-val{font-size:16px}
  footer{padding:20px 16px;flex-direction:column;align-items:flex-start}
  .footer-links,.footer-note{display:none}
}
</style>
</head>
<body>

<!-- ── Nav ─────────────────────────────────────────────────────────────── -->
<nav>
  <div class="nav-brand">
    <div class="nav-dot"></div>
    <span class="nav-name">NYSE Trading Engine</span>
  </div>
  <div class="nav-actions">
    {% if logged_in %}
    <a href="/dashboard" class="btn-solid">Open Dashboard &rarr;</a>
    {% else %}
    <a href="/register" class="btn-outline">Sign Up Free</a>
    <a href="/login" class="btn-solid">Log In</a>
    {% endif %}
  </div>
</nav>

<!-- ── Hero ─────────────────────────────────────────────────────────────── -->
<section class="hero">
  <div class="hero-eyebrow">
    <span class="eyebrow-dot"></span>
    Paper Trading · No Risk · Real Signals
  </div>
  <h1>Algorithmic Trading<br><span class="grad">for Everyone</span></h1>
  <p class="hero-sub">Professional-grade trading signals. Zero coding required.<br>Start paper trading in 5 minutes.</p>
  <div class="hero-cta">
    {% if logged_in %}
    <a href="/dashboard" class="btn-hero-p">Open Dashboard &rarr;</a>
    {% else %}
    <a href="/login" class="btn-hero-p">Start Paper Trading Free &rarr;</a>
    <a href="#how-it-works" class="btn-hero-s">See how it works</a>
    {% endif %}
    <a href="/leaderboard" class="btn-hero-s" style="border-color:rgba(16,185,129,.4);color:#6ee7b7">&#127942; See Our Track Record</a>
  </div>

  <!-- Stats bar -->
  <div class="stats-bar">
    <div class="stat-item">
      <span class="stat-val">10+</span>
      <span class="stat-sep">Indicators</span>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <span class="stat-val">Real-Time</span>
      <span class="stat-sep">Market Data</span>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <span class="stat-val">S&amp;P 500</span>
      <span class="stat-sep">Universe</span>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <span class="stat-val">$0</span>
      <span class="stat-sep">to Start</span>
    </div>
  </div>
</section>

<!-- ── Features ──────────────────────────────────────────────────────────── -->
<div id="features" class="section-wrap">
  <div class="section">
    <div class="eyebrow">What you get</div>
    <h2 class="sec-title">Everything in one dashboard</h2>
    <p class="sec-sub">Professional-grade tools with zero setup. Connect Alpaca, run the engine, watch it trade.</p>
    <div class="feat-grid">
      <div class="feat-card">
        <div class="feat-icon fi-blue">📡</div>
        <div class="feat-title">Real-Time Signal Analysis</div>
        <div class="feat-desc">RSI, MACD, EMA, Bollinger Bands, momentum, and mean-reversion signals — scored, weighted, and blended into a single composite score every minute.</div>
      </div>
      <div class="feat-card">
        <div class="feat-icon fi-purple">🧠</div>
        <div class="feat-title">Multi-Indicator Scoring</div>
        <div class="feat-desc">Eight independent signal types across three timeframes. A Gradient Boosting ML model re-ranks candidates based on your own trading history.</div>
      </div>
      <div class="feat-card">
        <div class="feat-icon fi-green">🏦</div>
        <div class="feat-title">Alpaca Paper Trading</div>
        <div class="feat-desc">Orders route directly through Alpaca's paper trading API — real market prices, real order logic, zero real money at risk. Flip to live when you're ready.</div>
      </div>
      <div class="feat-card">
        <div class="feat-icon fi-amber">📈</div>
        <div class="feat-title">Live Portfolio Tracking</div>
        <div class="feat-desc">Equity curve, open positions, unrealized P&amp;L, trailing stops, sector exposure, and a full trade journal updated in real time.</div>
      </div>
    </div>
  </div>
</div>

<!-- ── How it works ───────────────────────────────────────────────────────── -->
<div id="how-it-works" class="hiw">
  <div class="hiw-inner">
    <div class="eyebrow">How it works</div>
    <h2 class="sec-title">Up and running in three steps</h2>
    <div class="hiw-steps">
      <div class="hiw-step">
        <div class="hiw-num">1</div>
        <div class="hiw-title">Connect &amp; Configure</div>
        <div class="hiw-desc">Add your free Alpaca paper-trading API keys to <code style="background:rgba(59,130,246,.12);padding:1px 5px;border-radius:4px;font-size:12px">.env</code>. No broker account, no real money required. The engine starts in simulation mode if you skip this step.</div>
      </div>
      <div class="hiw-step">
        <div class="hiw-num">2</div>
        <div class="hiw-title">Scan &amp; Score</div>
        <div class="hiw-desc">Click <strong style="color:#e8edf5">Run Cycle</strong>. The engine scans the S&amp;P 500, scores every ticker across 8 signal types, and selects the 8–10 highest-conviction, non-correlated setups.</div>
      </div>
      <div class="hiw-step">
        <div class="hiw-num">3</div>
        <div class="hiw-title">Trade &amp; Relax</div>
        <div class="hiw-desc">Orders are placed automatically with adaptive position sizing. Trailing stops, take-profit targets, and a daily loss limit protect your paper portfolio around the clock.</div>
      </div>
    </div>
  </div>
</div>

<!-- ── Stats bar section ──────────────────────────────────────────────────── -->
<div class="proof">
  <div class="proof-grid">
    <div class="proof-card">
      <div class="proof-val pv-blue">10+</div>
      <div class="proof-lbl">Technical indicators tracked per ticker</div>
    </div>
    <div class="proof-card">
      <div class="proof-val pv-green">Real-Time</div>
      <div class="proof-lbl">Market data via yFinance &amp; Alpaca</div>
    </div>
    <div class="proof-card">
      <div class="proof-val pv-purple">S&amp;P 500</div>
      <div class="proof-lbl">Universe scanned each session</div>
    </div>
    <div class="proof-card">
      <div class="proof-val pv-amber">$0</div>
      <div class="proof-lbl">Real money needed to get started</div>
    </div>
  </div>
</div>

<!-- ── Footer ─────────────────────────────────────────────────────────────── -->
<footer>
  <div class="footer-brand">
    <div class="footer-dot"></div>
    NYSE Trading Engine
  </div>
  <div class="footer-links">
    <a href="/dashboard" class="footer-link">Dashboard</a>
    <a href="/login" class="footer-link">Login</a>
    <a href="/register" class="footer-link">Sign Up</a>
    <a href="/leaderboard" class="footer-link">Leaderboard</a>
    <a href="/stats" class="footer-link">Stats</a>
  </div>
  <div class="footer-note">For research and education only. Not financial advice. Past simulated performance does not guarantee future results.</div>
</footer>
</body>
</html>"""

# ── Login page template ────────────────────────────────────────────────────────
_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Login — NYSE Trading Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;
      padding:40px 36px;width:100%;max-width:380px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.logo{font-size:18px;font-weight:700;color:#38bdf8;margin-bottom:4px;text-align:center}
.sub{font-size:12px;color:#475569;text-align:center;margin-bottom:28px}
label{display:block;font-size:11px;color:#64748b;text-transform:uppercase;
      letter-spacing:.5px;margin-bottom:5px}
input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:7px;
      padding:10px 13px;color:#e2e8f0;font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#0ea5e9}
input::placeholder{color:#475569}
.btn{width:100%;background:#0ea5e9;color:#fff;border:none;border-radius:7px;
     padding:11px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn:hover{opacity:.88}
.error{background:#7f1d1d;color:#fca5a5;border-radius:7px;padding:9px 12px;
       font-size:13px;margin-bottom:16px;text-align:center}
</style>
</head>
<body>
<div class="card">
  <div class="logo">NYSE Trading Engine</div>
  <div class="sub">Sign in to access your dashboard</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post">
    <label>Username</label>
    <input name="username" type="text" autocomplete="username"
           placeholder="Enter username" autofocus required/>
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password"
           placeholder="Enter password" required/>
    <button class="btn" type="submit">Sign In</button>
  </form>
  <p style="text-align:center;margin-top:20px;font-size:13px;color:#475569">
    Don't have an account?
    <a href="/register" style="color:#38bdf8;font-weight:600">Sign up free</a>
  </p>
</div>
</body>
</html>"""

# ── Registration page template ─────────────────────────────────────────────────
_REGISTER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Sign Up — NYSE Trading Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;
     min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#1e293b;border:1px solid #334155;border-radius:14px;
      padding:40px 36px;width:100%;max-width:400px;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.logo{font-size:18px;font-weight:700;color:#38bdf8;margin-bottom:4px;text-align:center}
.sub{font-size:12px;color:#475569;text-align:center;margin-bottom:28px}
label{display:block;font-size:11px;color:#64748b;text-transform:uppercase;
      letter-spacing:.5px;margin-bottom:5px}
input{width:100%;background:#0f172a;border:1px solid #334155;border-radius:7px;
      padding:10px 13px;color:#e2e8f0;font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#0ea5e9}
input::placeholder{color:#475569}
.btn{width:100%;background:#0ea5e9;color:#fff;border:none;border-radius:7px;
     padding:11px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
.btn:hover{opacity:.88}
.error{background:#7f1d1d;color:#fca5a5;border-radius:7px;padding:9px 12px;
       font-size:13px;margin-bottom:16px;text-align:center}
.field-error{font-size:11px;color:#f87171;margin-top:-12px;margin-bottom:12px}
.hint{font-size:11px;color:#475569;margin-top:-12px;margin-bottom:14px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">NYSE Trading Engine</div>
  <div class="sub">Create a free account to get started</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post" novalidate>
    <label>Username</label>
    <input name="username" type="text" autocomplete="username"
           placeholder="Choose a username" value="{{ username or '' }}"
           autofocus required minlength="3" maxlength="40"/>
    {% if errors.username %}<div class="field-error">{{ errors.username }}</div>{% endif %}
    <label>Email</label>
    <input name="email" type="email" autocomplete="email"
           placeholder="you@example.com" value="{{ email or '' }}" required/>
    {% if errors.email %}<div class="field-error">{{ errors.email }}</div>{% endif %}
    <label>Password</label>
    <input name="password" type="password" autocomplete="new-password"
           placeholder="At least 8 characters" required minlength="8"/>
    {% if errors.password %}<div class="field-error">{{ errors.password }}</div>{% endif %}
    <label>Confirm Password</label>
    <input name="confirm" type="password" autocomplete="new-password"
           placeholder="Repeat your password" required/>
    {% if errors.confirm %}<div class="field-error">{{ errors.confirm }}</div>{% endif %}
    <button class="btn" type="submit">Create Account</button>
  </form>
  <p style="text-align:center;margin-top:20px;font-size:13px;color:#475569">
    Already have an account?
    <a href="/login" style="color:#38bdf8;font-weight:600">Sign in</a>
  </p>
</div>
</body>
</html>"""

# ── Authentication ─────────────────────────────────────────────────────────────
# Set DASH_USERNAME and DASH_PASSWORD in .env to enable login protection.
# Leave both blank (default) to run without authentication.
_DASH_USER = os.getenv("DASH_USERNAME", "").strip()
_DASH_PASS = os.getenv("DASH_PASSWORD", "").strip()
_AUTH_ENABLED = bool(_DASH_USER and _DASH_PASS)

# Secret key signs session cookies. Set DASH_SECRET_KEY in .env for persistence
# across restarts; otherwise a random key is generated (sessions reset on restart).
app.secret_key = os.getenv("DASH_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

logging.info(
    "[AUTH] enabled=%s user=%r secret_key_set=%s",
    _AUTH_ENABLED,
    _DASH_USER or "(not set)",
    bool(os.getenv("DASH_SECRET_KEY")),
)

# Routes exempt from auth — public pages and static assets
_PUBLIC_ENDPOINTS = {"login", "logout", "register", "home", "pwa_manifest", "pwa_icon",
                     "service_worker", "leaderboard_page", "api_leaderboard"}


@app.before_request
def _require_login():
    endpoint = request.endpoint or ""
    path = request.path
    logged_in = bool(session.get("logged_in"))

    logging.info(
        "[REQ] %s %s | endpoint=%s logged_in=%s auth_enabled=%s",
        request.method, path, endpoint, logged_in, _AUTH_ENABLED,
    )

    if not _AUTH_ENABLED:
        logging.info("[AUTH] skipped — auth not enabled")
        return

    if endpoint in _PUBLIC_ENDPOINTS:
        logging.info("[AUTH] allowed — public endpoint")
        return

    if not logged_in:
        if path.startswith("/api/"):
            logging.info("[AUTH] → 401 JSON (api, not logged in)")
            return jsonify({"ok": False, "error": "Not authenticated"}), 401
        logging.info("[AUTH] → redirect /login (not logged in)")
        return redirect(f"/login?next={path}")

    logging.info("[AUTH] allowed — logged in")


def _ensure_admin_in_db() -> None:
    """Create the admin account in the DB from env vars if it doesn't exist yet."""
    if not (_DASH_USER and _DASH_PASS):
        return
    with app.app_context():
        if not User.query.filter_by(username=_DASH_USER).first():
            admin = User(
                username=_DASH_USER,
                email=f"{_DASH_USER}@admin.local",
                password_hash=generate_password_hash(_DASH_PASS),
            )
            db.session.add(admin)
            db.session.commit()
            logging.info("[AUTH] Admin account created in DB: %r", _DASH_USER)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        # ── Check env-var admin credentials first ─────────────────────────────
        is_env_admin = (
            _DASH_USER and _DASH_PASS
            and secrets.compare_digest(username, _DASH_USER)
            and secrets.compare_digest(password, _DASH_PASS)
        )
        if is_env_admin:
            _ensure_admin_in_db()

        # ── Look up user in database ──────────────────────────────────────────
        db_user = User.query.filter_by(username=username).first()
        authenticated = is_env_admin or (
            db_user is not None and check_password_hash(db_user.password_hash, password)
        )

        if authenticated:
            # Re-fetch in case _ensure_admin_in_db() just created the row
            if db_user is None:
                db_user = User.query.filter_by(username=username).first()
            session.permanent = True
            session["logged_in"] = True
            session["user_id"] = db_user.id if db_user else None
            next_url = request.args.get("next", "/dashboard")
            if not next_url.startswith("/"):
                next_url = "/dashboard"
            logging.info("[AUTH] Login OK: %r (id=%s)", username, session["user_id"])
            return redirect(next_url)

        error = "Invalid username or password."
        logging.info("[AUTH] Login FAILED: %r", username)

    return render_template_string(_LOGIN_HTML, error=error, auth=_AUTH_ENABLED)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template_string(_REGISTER_HTML, errors={}, username="", email="")

    username = request.form.get("username", "").strip()
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    errors: dict = {}

    if len(username) < 3:
        errors["username"] = "Username must be at least 3 characters."
    elif len(username) > 40:
        errors["username"] = "Username must be 40 characters or fewer."
    elif not username.replace("_", "").replace("-", "").isalnum():
        errors["username"] = "Username may only contain letters, numbers, hyphens, and underscores."

    if not email or "@" not in email:
        errors["email"] = "Enter a valid email address."

    if len(password) < 8:
        errors["password"] = "Password must be at least 8 characters."

    if password != confirm:
        errors["confirm"] = "Passwords do not match."

    if not errors:
        with app.app_context():
            if User.query.filter_by(username=username).first():
                errors["username"] = "That username is already taken."
            elif User.query.filter_by(email=email).first():
                errors["email"] = "An account with that email already exists."

    if errors:
        return render_template_string(_REGISTER_HTML, errors=errors,
                                      username=username, email=email)

    user = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
    )
    db.session.add(user)
    db.session.commit()
    logging.info("[REGISTER] New user: %r (%s)", username, email)
    return redirect("/login")


_lock = threading.Lock()
_engine = TradingEngine(config)        # admin / shared engine
_user_engines: dict = {}               # user_id → TradingEngine
_ue_lock = threading.Lock()            # guards _user_engines dict
_last_state: dict = {}
_last_cycle_at: Optional[datetime] = None
_next_cycle_at: Optional[datetime] = None
_equity_snapshots: list = []          # [{ts, value}] — portfolio value over time
_last_snapshot_ts: Optional[datetime] = None
_ext_hours = ExtendedHoursMonitor(cache_ttl_seconds=120)


def _create_user_engine(user_id: int) -> TradingEngine:
    """Build a TradingEngine configured with the user's stored Alpaca keys."""
    user = db.session.get(User, user_id)
    cfg = TradingConfig()
    if user:
        api_key    = _decrypt_key(user.alpaca_api_key_enc or "")
        secret_key = _decrypt_key(user.alpaca_secret_key_enc or "")
        if api_key and secret_key:
            cfg.use_alpaca       = True
            cfg.alpaca_api_key   = api_key
            cfg.alpaca_secret_key = secret_key
            cfg.paper_trading    = bool(user.alpaca_paper)
    journal_dir = Path(__file__).resolve().parent / "journals"
    journal_dir.mkdir(exist_ok=True)
    eng = TradingEngine(cfg)
    eng.journal = TradeJournal(journal_dir / f"user_{user_id}.jsonl")
    return eng


def _get_engine() -> TradingEngine:
    """Return the TradingEngine for the currently logged-in user."""
    if not _AUTH_ENABLED:
        return _engine
    user_id = session.get("user_id")
    if not user_id:
        return _engine
    with _ue_lock:
        if user_id not in _user_engines:
            _user_engines[user_id] = _create_user_engine(user_id)
        return _user_engines[user_id]


def _invalidate_user_engine(user_id: int) -> None:
    """Drop a cached user engine so it is recreated on next request."""
    with _ue_lock:
        _user_engines.pop(user_id, None)

# ── Risk profiles / settings ──────────────────────────────────────────────────
_SETTINGS_PATH = Path(__file__).resolve().parent / "user_settings.json"

RISK_PROFILES = {
    "conservative": {
        "label": "Conservative",
        "tagline": "Lower risk, smaller positions, only strong signals",
        "color": "#10b981",
        "score_label": "8+",
        "overrides": {
            "max_position_pct": 0.02,
            "min_position_pct": 0.01,
            "max_open_positions": 3,
            "stop_loss_pct": 0.03,
            "take_profit_pct": 0.10,
            "trailing_stop_pct": 0.03,
            "daily_loss_limit_pct": 0.02,
            "buy_threshold": 0.35,
            "sell_threshold": -0.35,
        },
    },
    "moderate": {
        "label": "Moderate",
        "tagline": "Balanced defaults — current behaviour",
        "color": "#3b82f6",
        "score_label": "6+",
        "overrides": {
            "max_position_pct": 0.05,
            "min_position_pct": 0.02,
            "max_open_positions": 5,
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.15,
            "trailing_stop_pct": 0.05,
            "daily_loss_limit_pct": 0.03,
            "buy_threshold": 0.20,
            "sell_threshold": -0.20,
        },
    },
    "aggressive": {
        "label": "Aggressive",
        "tagline": "Larger positions, wider stops, acts on weaker signals",
        "color": "#f59e0b",
        "score_label": "4+",
        "overrides": {
            "max_position_pct": 0.10,
            "min_position_pct": 0.03,
            "max_open_positions": 8,
            "stop_loss_pct": 0.08,
            "take_profit_pct": 0.25,
            "trailing_stop_pct": 0.08,
            "daily_loss_limit_pct": 0.05,
            "buy_threshold": 0.10,
            "sell_threshold": -0.10,
        },
    },
}
_current_profile: str = "moderate"


def _load_user_settings() -> dict:
    if _SETTINGS_PATH.exists():
        try:
            return json.loads(_SETTINGS_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_user_settings(data: dict) -> None:
    try:
        _SETTINGS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning(f"Failed to save user settings: {e}")


def _apply_risk_profile(name: str) -> bool:
    global _current_profile
    profile = RISK_PROFILES.get(name)
    if not profile:
        return False
    for key, val in profile["overrides"].items():
        if hasattr(_engine.config, key):
            setattr(_engine.config, key, val)
    _current_profile = name
    log.info(f"[SETTINGS] Risk profile applied: {name}")
    return True


# Apply saved profile at startup
_saved = _load_user_settings()
if _saved.get("risk_profile") in RISK_PROFILES:
    _apply_risk_profile(_saved["risk_profile"])
else:
    _current_profile = "moderate"
_engine.emailer.active = bool(_saved.get("email_notifications", False))

# ── News feed cache ───────────────────────────────────────────────────────────
_news_cache: dict = {}       # {symbol: {"items": [...], "fetched_at": datetime}}
_NEWS_CACHE_TTL = 900        # 15-minute TTL

# ── Weekend backtest state ────────────────────────────────────────────────────
_backtest_report: dict = {}
_backtest_running: bool = False
_BACKTEST_REPORT_PATH = Path(__file__).resolve().parent / "backtest_report.json"

# ── Personal watchlist ────────────────────────────────────────────────────────
_PERSONAL_WL_PATH = Path(__file__).resolve().parent / "personal_watchlist.json"
_personal_watchlist: list = []


def _load_personal_watchlist() -> None:
    global _personal_watchlist
    if _PERSONAL_WL_PATH.exists():
        try:
            with open(_PERSONAL_WL_PATH) as f:
                _personal_watchlist = [s.upper().strip() for s in json.load(f) if s]
        except Exception:
            _personal_watchlist = []


def _save_personal_watchlist() -> None:
    with open(_PERSONAL_WL_PATH, "w") as f:
        json.dump(_personal_watchlist, f)


_load_personal_watchlist()

# ── Public ngrok URL ──────────────────────────────────────────────────────────
_public_url: str = ""

log = logging.getLogger(__name__)


def _record_snapshot(total_value: float) -> None:
    """Append an equity snapshot at most once per minute."""
    global _last_snapshot_ts
    now = datetime.now()
    if _last_snapshot_ts and (now - _last_snapshot_ts).total_seconds() < 60:
        return
    _equity_snapshots.append({"ts": now.isoformat(), "value": round(total_value, 2)})
    _last_snapshot_ts = now
    if len(_equity_snapshots) > 2000:        # ~33 hours at 1/min
        del _equity_snapshots[:-2000]


def _background_loop() -> None:
    """Daemon thread: run a full trading cycle every CYCLE_INTERVAL seconds."""
    global _last_cycle_at, _next_cycle_at
    # Short warm-up so the server is ready before the first cycle fires
    _next_cycle_at = datetime.now() + timedelta(seconds=10)
    time.sleep(10)
    while True:
        with _lock:
            try:
                _engine.run_cycle()
                _last_cycle_at = datetime.now()
            except Exception as e:
                log.error(f"Background cycle error: {e}")
        # Auto-trigger weekend backtest on Fridays after market close (≥16:00)
        _now = datetime.now()
        if _now.weekday() == 4 and _now.hour >= 16 and not _backtest_running:
            last_end = _backtest_report.get("period_end") if _backtest_report else None
            if last_end != _now.date().isoformat():
                threading.Thread(target=_run_backtest_bg, daemon=True,
                                 name="friday-backtest").start()
                log.info("Friday auto-backtest triggered")
        _next_cycle_at = datetime.now() + timedelta(seconds=CYCLE_INTERVAL)
        time.sleep(CYCLE_INTERVAL)

# ── State builder ─────────────────────────────────────────────────────────────

def _safe_empty_state(error: str = "") -> dict:
    """Minimal valid state returned when _build_state fails completely."""
    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "Local Simulation",
        "market_open": None,
        "portfolio": {
            "total_value": 0, "cash": 0, "position_value": 0,
            "total_pnl": 0, "total_pnl_pct": 0,
            "open_positions": 0, "total_trades": 0, "initial_capital": 0,
        },
        "positions": [], "signals": [], "trades": [],
        "error": error,
        "last_cycle_at": None, "next_cycle_at": None,
        "cycle_interval": CYCLE_INTERVAL,
        "watchlist": [], "scan": None, "voo": None,
        "notifications": {"ntfy": False, "pushover": False},
        "mtf_enabled": False, "sector_exposure": {},
        "max_per_sector": 3, "earnings_enabled": False, "earnings_warnings": {},
        "trailing_stop_enabled": False, "confirmation_enabled": False,
        "pending_confirmation": [], "extended_hours": [],
        "mean_reversion_enabled": False, "correlation_filter_enabled": False,
        "adaptive_sizing_enabled": False, "regime": None, "ml_status": None,
        "public_url": None, "personal_watchlist": [],
        "alpaca_connected": False,
    }


def _build_state(signals=None, prices=None, ind_map=None, error=None) -> dict:
    eng = _get_engine()
    portfolio = eng.portfolio
    price_lookup = prices or {}

    # ── Positions — pull live from Alpaca when enabled ────────────────────────
    pos_list = []
    if eng.config.use_alpaca:
        try:
            for p in eng.executor.get_live_positions():
                entry = float(p["entry_price"])
                cp = float(p["current_price"] or entry)
                pnl = float(p["pnl"]) if p["pnl"] is not None else (cp - entry) * float(p["shares"])
                pnl_pct = float(p["pnl_pct"]) * 100 if p["pnl_pct"] is not None else (
                    (cp - entry) / entry * 100 if entry else 0
                )
                sym = p["symbol"]
                local_pos = portfolio.positions.get(sym)
                if local_pos and eng.config.use_trailing_stop:
                    trail_stop = round(local_pos.stop_loss, 2)
                    highest = round(local_pos.highest_price, 2)
                else:
                    trail_stop = round(entry * (1 - eng.config.stop_loss_pct), 2)
                    highest = None
                pos_list.append({
                    "symbol": sym,
                    "shares": round(float(p["shares"]), 4),
                    "entry_price": round(entry, 2),
                    "current_price": round(cp, 2),
                    "stop_loss": trail_stop,
                    "highest_price": highest,
                    "take_profit": round(entry * (1 + eng.config.take_profit_pct), 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "sector": get_sector(sym) or "—",
                })
        except Exception as e:
            log.warning(f"Alpaca live positions failed: {e}")

    # Fallback: local portfolio mirror (Local Simulation or Alpaca fetch failed)
    if not pos_list:
        for sym, pos in portfolio.positions.items():
            cp = price_lookup.get(sym, pos.entry_price)
            pos_list.append({
                "symbol": sym,
                "shares": round(pos.shares, 4),
                "entry_price": round(pos.entry_price, 2),
                "current_price": round(cp, 2),
                "stop_loss": round(pos.stop_loss, 2),
                "highest_price": round(pos.highest_price, 2) if eng.config.use_trailing_stop else None,
                "take_profit": round(pos.take_profit, 2),
                "pnl": round(pos.unrealized_pnl(cp), 2),
                "pnl_pct": round(pos.unrealized_pnl_pct(cp) * 100, 2),
                "sector": get_sector(sym) or "—",
            })

    sector_exposure: dict = {}
    for p in pos_list:
        sec = p["sector"]
        if sec and sec != "—":
            sector_exposure[sec] = sector_exposure.get(sec, 0) + 1

    # ── Trades — pull filled orders from Alpaca when enabled ──────────────────
    trades_list = []
    if eng.config.use_alpaca:
        try:
            trades_list = eng.executor.get_filled_orders(limit=30)
        except Exception as e:
            log.warning(f"Alpaca orders fetch failed: {e}")

    if not trades_list:
        trades_list = [
            {
                "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "action": t.action,
                "symbol": t.symbol,
                "shares": round(t.shares, 4),
                "price": round(t.price, 2),
                "pnl": round(t.pnl, 2) if t.pnl is not None else None,
                "pnl_pct": round(t.pnl_pct * 100, 2) if t.pnl_pct is not None else None,
                "reason": t.reason,
            }
            for t in reversed(portfolio.trades[-30:])
        ]

    # ── Portfolio summary — prefer Alpaca account data ────────────────────────
    summary = None
    if eng.config.use_alpaca:
        try:
            acct = eng.executor.get_account_summary()
            total_pnl = acct["portfolio_value"] - eng.config.initial_capital
            summary = {
                "total_value": acct["portfolio_value"],
                "cash": acct["cash"],
                "position_value": acct["portfolio_value"] - acct["cash"],
                "total_pnl": total_pnl,
                "total_pnl_pct": (total_pnl / eng.config.initial_capital * 100) if eng.config.initial_capital else 0,
                "open_positions": len(pos_list),
                "total_trades": len(trades_list),
            }
        except Exception as e:
            log.warning(f"Alpaca account fetch failed: {e}")

    if summary is None:
        summary = portfolio.get_summary(price_lookup) if price_lookup else {
            "total_value": portfolio.cash,
            "cash": portfolio.cash,
            "position_value": 0,
            "total_pnl": 0,
            "total_pnl_pct": 0,
            "open_positions": len(pos_list),
            "total_trades": len(trades_list),
        }

    _record_snapshot(summary["total_value"])

    # ── Signals ───────────────────────────────────────────────────────────────
    corr_blocked = eng.last_corr_blocked   # {sym: reason}

    sig_list = []
    if signals:
        for sym, sig in signals.items():
            ind = ind_map.get(sym) if ind_map else None
            iscores = sig.indicator_scores or {}
            vol_ratio = None
            if ind and ind.volume and ind.avg_volume and ind.avg_volume > 0:
                vol_ratio = round(ind.volume / ind.avg_volume, 2)
            # Estimated adaptive position size for BUY signals
            est_size_pct = None
            if sig.action == "BUY" and ind and ind.atr_pct:
                raw = eng.risk.compute_position_pct(sig.confidence, ind.atr_pct)
                est_size_pct = round(raw * 100, 1)
            sig_list.append({
                "symbol": sym,
                "price": round(price_lookup.get(sym, 0), 2),
                "action": sig.action,
                "score": round(sig.score, 3),
                "confidence": round(sig.confidence, 3),
                "rsi": round(ind.rsi, 1) if ind and ind.rsi else None,
                "z_score": round(ind.z_score, 2) if ind and ind.z_score is not None else None,
                "atr_pct": round(ind.atr_pct * 100, 2) if ind and ind.atr_pct else None,
                "volume_ratio": vol_ratio,
                "est_size_pct": est_size_pct,
                "corr_blocked": corr_blocked.get(sym),
                "reasons": sig.reasons,
                "macd_hist":      round(ind.macd_hist, 4)      if ind and ind.macd_hist      is not None else None,
                "macd_hist_prev": round(ind.macd_hist_prev, 4) if ind and ind.macd_hist_prev is not None else None,
                "ema_fast":       round(ind.ema_fast, 2)       if ind and ind.ema_fast       else None,
                "ema_slow":       round(ind.ema_slow, 2)       if ind and ind.ema_slow       else None,
                "bb_upper":       round(ind.bb_upper, 2)       if ind and ind.bb_upper       else None,
                "bb_lower":       round(ind.bb_lower, 2)       if ind and ind.bb_lower       else None,
                "roc_10":         round(ind.roc_10, 4)         if ind and getattr(ind, "roc_10",    None) is not None else None,
                "stoch_rsi":      round(ind.stoch_rsi, 1)      if ind and getattr(ind, "stoch_rsi", None) is not None else None,
                "tf_1d":  round(iscores["1d"],  3) if "1d"  in iscores else None,
                "tf_1h":  round(iscores["1h"],  3) if "1h"  in iscores else None,
                "tf_15m": round(iscores["15m"], 3) if "15m" in iscores else None,
                "mtf_agreement": int(iscores["mtf_agreement"]) if "mtf_agreement" in iscores else None,
                "ml_mult": round(iscores["ml_mult"], 3) if "ml_mult" in iscores else None,
                "sector": get_sector(sym) or "—",
            })
        sig_list.sort(key=lambda x: -abs(x["score"]))

    # ── Earnings warnings ─────────────────────────────────────────────────────
    earnings_warnings: dict = {}
    if eng.earnings_cal:
        for sym in eng.watchlist:
            try:
                if eng.earnings_cal.has_upcoming_earnings(sym):
                    cached_dt = eng.earnings_cal._cache.get(sym)
                    if cached_dt:
                        days_away = max(0, (cached_dt - datetime.now()).days)
                        earnings_warnings[sym] = days_away
            except Exception:
                pass

    mode = (
        "Alpaca Paper" if eng.config.use_alpaca and eng.config.paper_trading
        else "Alpaca LIVE" if eng.config.use_alpaca
        else "Local Simulation"
    )

    market_open = None
    if eng.config.use_alpaca:
        try:
            market_open = eng.executor.is_market_open()
        except Exception:
            market_open = None

    # ── Extended hours ────────────────────────────────────────────────────────
    ext_hours = []
    try:
        ext_hours = _ext_hours.fetch(eng.watchlist)
    except Exception as e:
        log.debug(f"Extended hours fetch error: {e}")

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "market_open": market_open,
        "portfolio": {
            "total_value": round(summary["total_value"], 2),
            "cash": round(summary["cash"], 2),
            "position_value": round(summary["position_value"], 2),
            "total_pnl": round(summary["total_pnl"], 2),
            "total_pnl_pct": round(summary["total_pnl_pct"], 2),
            "open_positions": summary["open_positions"],
            "total_trades": summary["total_trades"],
            "initial_capital": eng.config.initial_capital,
        },
        "positions": pos_list,
        "signals": sig_list,
        "trades": trades_list,
        "error": error,
        "last_cycle_at": _last_cycle_at.isoformat() if _last_cycle_at else None,
        "next_cycle_at": _next_cycle_at.isoformat() if _next_cycle_at else None,
        "cycle_interval": CYCLE_INTERVAL,
        "watchlist": eng.watchlist,
        "scan": eng.scanner.last_result.to_dict() if eng.scanner.last_result else None,
        "voo": eng.voo_monitor.last_status.to_dict() if eng.voo_monitor.last_status else None,
        "notifications": {
            "ntfy": bool(eng.config.ntfy_topic),
            "pushover": bool(eng.config.pushover_token and eng.config.pushover_user),
        },
        "mtf_enabled": eng.config.use_multi_timeframe,
        "sector_exposure": sector_exposure,
        "max_per_sector": eng.config.max_positions_per_sector,
        "earnings_enabled": eng.config.use_earnings_protection,
        "earnings_warnings": earnings_warnings,
        "trailing_stop_enabled": eng.config.use_trailing_stop,
        "confirmation_enabled": eng.config.use_confirmation,
        "pending_confirmation": list(eng.pending_confirmations.keys()),
        "extended_hours": ext_hours,
        "mean_reversion_enabled": eng.config.use_mean_reversion,
        "correlation_filter_enabled": eng.config.use_correlation_filter,
        "adaptive_sizing_enabled": eng.config.use_adaptive_sizing,
        "regime": eng.current_regime.to_dict() if eng.current_regime else None,
        "ml_status": eng.ml_status,
        "public_url": _public_url,
        "personal_watchlist": _personal_watchlist,
        "alpaca_connected": eng.config.use_alpaca,
    }


def _trade_stats() -> dict:
    """Compute win/loss stats from the in-memory trade history."""
    sells = [t for t in _get_engine().portfolio.trades if t.action == "SELL" and t.pnl is not None]
    if not sells:
        return {"sell_trades": 0, "win_rate": None, "avg_gain": None, "avg_loss": None,
                "best_trade": None, "worst_trade": None, "total_realized_pnl": 0.0}
    pnls = [t.pnl for t in sells]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p <= 0]
    return {
        "sell_trades":        len(sells),
        "win_rate":           round(len(winners) / len(sells) * 100, 1),
        "avg_gain":           round(sum(winners) / len(winners), 2) if winners else 0,
        "avg_loss":           round(sum(losers)  / len(losers),  2) if losers  else 0,
        "best_trade":         round(max(pnls), 2),
        "worst_trade":        round(min(pnls), 2),
        "total_realized_pnl": round(sum(pnls), 2),
    }


def _compute_risk_metrics() -> dict:
    """Sharpe ratio, max/current drawdown, Calmar ratio from equity snapshots."""
    import math

    empty = {"sharpe": None, "max_drawdown_pct": None, "max_drawdown_dollar": None,
             "current_drawdown_pct": None, "calmar": None, "data_days": 0,
             "drawdown_curve": []}
    if len(_equity_snapshots) < 2:
        return empty

    values = [s["value"] for s in _equity_snapshots]

    # ── Max drawdown — walk equity curve tracking running peak ────────────────
    peak = values[0]
    max_dd_pct = 0.0
    max_dd_dollar = 0.0
    dd_curve = []                   # (timestamp, drawdown_pct) for chart
    for snap in _equity_snapshots:
        v = snap["value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
            max_dd_dollar = peak - v
        dd_curve.append({"ts": snap["ts"], "dd": round(-dd * 100, 3)})

    all_time_high = max(values)
    current_dd = (all_time_high - values[-1]) / all_time_high if all_time_high > 0 else 0.0

    # ── Group by calendar day (last snapshot per day wins) ────────────────────
    daily: dict = {}
    for snap in _equity_snapshots:
        daily[snap["ts"][:10]] = snap["value"]
    data_days = len(daily)
    day_vals = [daily[d] for d in sorted(daily)]

    # ── Sharpe ratio — prefer daily returns; fall back to sub-minute ──────────
    sharpe = None
    if len(day_vals) >= 3:
        rets = [day_vals[i] / day_vals[i - 1] - 1
                for i in range(1, len(day_vals)) if day_vals[i - 1] > 0]
        if len(rets) >= 2:
            mu = sum(rets) / len(rets)
            sigma = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
            if sigma > 0:
                sharpe = round((mu - 0.05 / 252) / sigma * math.sqrt(252), 3)
    elif len(values) >= 4:
        rets = [values[i] / values[i - 1] - 1
                for i in range(1, len(values)) if values[i - 1] > 0]
        if len(rets) >= 3:
            mu = sum(rets) / len(rets)
            sigma = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))
            if sigma > 0:
                # snapshots ≈ 1 min apart → 390 min/day × 252 days/year
                sharpe = round((mu - 0.05 / (390 * 252)) / sigma * math.sqrt(390 * 252), 3)

    # ── Calmar ratio (annualised return ÷ max drawdown) ───────────────────────
    calmar = None
    if max_dd_pct > 0 and data_days >= 1 and values[0] > 0:
        total_ret = values[-1] / values[0] - 1
        ann_ret = (1 + total_ret) ** (252 / max(data_days, 1)) - 1
        calmar = round(ann_ret / max_dd_pct, 3)

    # Thin the drawdown curve to at most 400 points for the JSON payload
    step = max(1, len(dd_curve) // 400)
    thin_curve = dd_curve[::step]
    if dd_curve and thin_curve[-1] != dd_curve[-1]:
        thin_curve.append(dd_curve[-1])

    return {
        "sharpe":               sharpe,
        "max_drawdown_pct":     round(max_dd_pct * 100, 2),
        "max_drawdown_dollar":  round(max_dd_dollar, 2),
        "current_drawdown_pct": round(current_dd * 100, 2),
        "calmar":               calmar,
        "data_days":            data_days,
        "drawdown_curve":       thin_curve,
    }


# ── News feed helpers ─────────────────────────────────────────────────────────

def _parse_news_item(n: dict, sym: str) -> dict:
    """Normalise yfinance news dict (old flat format or new nested 'content' format)."""
    if "content" in n:
        c = n["content"]
        pub = c.get("pubDate", "")
        ts = 0
        if pub:
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                ts = int(dt.timestamp())
            except Exception:
                pass
        return {
            "symbol": sym,
            "title": c.get("title", ""),
            "publisher": (c.get("provider") or {}).get("displayName", ""),
            "url": (c.get("canonicalUrl") or {}).get("url", ""),
            "published_at": ts,
        }
    return {
        "symbol": sym,
        "title": n.get("title", ""),
        "publisher": n.get("publisher", ""),
        "url": n.get("link", ""),
        "published_at": int(n.get("providerPublishTime") or 0),
    }


# ── Backtest background runner ────────────────────────────────────────────────

def _run_backtest_bg() -> None:
    global _backtest_report, _backtest_running
    if _backtest_running:
        return
    _backtest_running = True
    try:
        from src.backtest.backtester import Backtester
        from datetime import date, timedelta as td

        end_d   = date.today()
        start_d = end_d - td(days=182)
        end_s   = end_d.isoformat()
        start_s = start_d.isoformat()

        symbols = _engine.watchlist or config.symbols
        bt      = Backtester(config)
        metrics = bt.run(symbols, start_s, end_s)

        # SPY buy-and-hold benchmark
        spy_return = None
        try:
            import yfinance as yf
            spy = yf.download("SPY", start=start_s, end=end_s,
                              progress=False, auto_adjust=True)
            if len(spy) > 1:
                spy_return = round(
                    float(spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1) * 100, 2
                )
        except Exception as e:
            log.debug(f"SPY benchmark fetch: {e}")

        report = {
            "generated_at":   datetime.now().isoformat(),
            "period_start":   start_s,
            "period_end":     end_s,
            "symbols":        symbols,
            "algo": {
                "total_return_pct":      round(metrics.total_return_pct, 2),
                "annualized_return_pct": round(metrics.annualized_return_pct, 2),
                "sharpe_ratio":          round(metrics.sharpe_ratio, 3) if metrics.sharpe_ratio else None,
                "max_drawdown_pct":      round(metrics.max_drawdown_pct, 2),
                "win_rate_pct":          round(metrics.win_rate_pct, 1),
                "profit_factor":         round(metrics.profit_factor, 3) if metrics.profit_factor else None,
                "total_trades":          metrics.total_trades,
            },
            "spy_return_pct": spy_return,
            "beats_spy":      (metrics.total_return_pct > spy_return)
                              if spy_return is not None else None,
        }

        _backtest_report = report
        with open(_BACKTEST_REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
        log.info(
            f"Backtest done: algo={metrics.total_return_pct:.1f}%  "
            f"SPY={spy_return}%  beats={'yes' if report['beats_spy'] else 'no'}"
        )
    except Exception as e:
        log.error(f"Backtest error: {e}")
        _backtest_report = {"error": str(e), "generated_at": datetime.now().isoformat()}
    finally:
        _backtest_running = False


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/state")
def api_state():
    global _last_state
    # If the engine lock is held by a running cycle, return the last cached
    # state immediately so the Refresh button never hangs.
    acquired = _lock.acquire(timeout=3)
    if not acquired:
        cached = dict(_last_state)
        cached["cycle_running"] = True
        return jsonify(cached)
    try:
        try:
            eng = _get_engine()
            signals, prices, ind_map = eng.get_signals()
            _last_state = _build_state(signals, prices, ind_map)
        except Exception as e:
            try:
                _last_state = _build_state(error=str(e))
            except Exception:
                _last_state = _safe_empty_state(str(e))
    finally:
        _lock.release()
    return jsonify(_last_state)


@app.route("/api/cycle", methods=["POST"])
def api_cycle():
    global _last_state
    with _lock:
        try:
            _get_engine().run_cycle()
            _last_state = _build_state(error=None)
            return jsonify({"ok": True, "state": _last_state})
        except Exception as e:
            err = traceback.format_exc()
            return jsonify({"ok": False, "error": str(e), "detail": err}), 500


@app.route("/api/voo", methods=["POST"])
def api_voo():
    try:
        status = _get_engine().voo_monitor.check(force=True)
        return jsonify({"ok": True, "voo": status.to_dict() if status else None})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pnl")
def api_pnl():
    """Lightweight endpoint — reads from cached state, no market data fetch."""
    with _lock:
        p = _last_state.get("portfolio", {})
        positions = _last_state.get("positions", [])
    if not p:
        return jsonify({"ok": False, "reason": "no data yet"})
    unrealized = sum(pos.get("pnl") or 0 for pos in positions)
    basis = (p.get("total_value") or 0) - unrealized
    unrealized_pct = round(unrealized / basis * 100, 2) if basis > 0 else 0.0
    return jsonify({
        "ok": True,
        "unrealized_pnl": round(unrealized, 2),
        "unrealized_pnl_pct": unrealized_pct,
        "total_pnl": p.get("total_pnl"),
        "total_pnl_pct": p.get("total_pnl_pct"),
        "open_positions": len(positions),
        "ts": _last_state.get("timestamp"),
    })


@app.route("/api/heatmap")
def api_heatmap():
    """Return daily price-change data for all watchlist symbols (uses fetcher cache)."""
    eng = _get_engine()
    watchlist = eng.watchlist
    if not watchlist:
        return jsonify({"ok": True, "items": []})
    try:
        market_data = eng.fetcher.fetch_many(watchlist, force_refresh=False)
        items = []
        for sym in watchlist:
            df = market_data.get(sym)
            if df is None or len(df) < 2:
                continue
            close = df["Close"]
            prev_close = float(close.iloc[-2])
            curr_close = float(close.iloc[-1])
            change_pct = round((curr_close / prev_close - 1) * 100, 2) if prev_close > 0 else 0.0
            vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else None
            avg_vol = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else None
            vol_ratio = round(vol / avg_vol, 2) if vol and avg_vol and avg_vol > 0 else None
            items.append({
                "symbol": sym,
                "price": round(curr_close, 2),
                "change_pct": change_pct,
                "volume_ratio": vol_ratio,
            })
        items.sort(key=lambda x: -abs(x["change_pct"]))
        return jsonify({"ok": True, "items": items})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    global _last_state
    with _lock:
        try:
            eng = _get_engine()
            eng.refresh_watchlist()
            signals, prices, ind_map = eng.get_signals()
            _last_state = _build_state(signals, prices, ind_map)
            return jsonify({"ok": True, "state": _last_state})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stats")
def api_stats():
    eng = _get_engine()
    prices = {}
    try:
        _, prices, _ = eng.get_signals()
    except Exception:
        pass
    portfolio = eng.portfolio
    summary   = portfolio.get_summary(prices) if prices else {"total_value": portfolio.cash}

    all_trades = [
        {
            "timestamp": t.timestamp.strftime("%Y-%m-%d %H:%M"),
            "action":    t.action,
            "symbol":    t.symbol,
            "shares":    round(t.shares, 4),
            "price":     round(t.price, 2),
            "pnl":       round(t.pnl,     2) if t.pnl     is not None else None,
            "pnl_pct":   round(t.pnl_pct * 100, 2) if t.pnl_pct is not None else None,
            "reason":    t.reason,
        }
        for t in reversed(portfolio.trades)
    ]

    # ── Period P&L breakdown ──────────────────────────────────────────────────
    import collections
    from datetime import date, timedelta

    sells = [t for t in portfolio.trades if t.action == "SELL" and t.pnl is not None]
    daily_map: dict = collections.defaultdict(float)
    weekly_map: dict = collections.defaultdict(float)
    monthly_map: dict = collections.defaultdict(float)
    for t in sells:
        d = t.timestamp.date()
        daily_map[d.isoformat()] += t.pnl
        yr, wk, _ = d.isocalendar()
        weekly_map[f"{yr}-W{wk:02d}"] += t.pnl
        monthly_map[f"{d.year}-{d.month:02d}"] += t.pnl

    today = datetime.now().date()
    daily_pnl  = [{"period": (today - timedelta(days=i)).isoformat(),
                   "pnl": round(daily_map.get((today - timedelta(days=i)).isoformat(), 0.0), 2)}
                  for i in range(29, -1, -1)]
    weekly_pnl = sorted(
        [{"period": k, "pnl": round(v, 2)} for k, v in weekly_map.items()],
        key=lambda x: x["period"])[-12:]
    monthly_pnl = sorted(
        [{"period": k, "pnl": round(v, 2)} for k, v in monthly_map.items()],
        key=lambda x: x["period"])[-12:]

    return jsonify({
        "trade_stats":      _trade_stats(),
        "equity_snapshots": _equity_snapshots,
        "initial_capital":  eng.config.initial_capital,
        "current_value":    round(summary["total_value"], 2),
        "trades":           all_trades,
        "period_pnl":       {"daily": daily_pnl, "weekly": weekly_pnl, "monthly": monthly_pnl},
        "risk_metrics":     _compute_risk_metrics(),
        "notifications": {
            "ntfy_enabled":      bool(eng.config.ntfy_topic),
            "ntfy_topic":        eng.config.ntfy_topic,
            "pushover_enabled":  bool(eng.config.pushover_token and eng.config.pushover_user),
        },
    })


def _compute_sr_levels(df, n_swing: int = 5, cluster_pct: float = 0.008, max_each: int = 3):
    """Identify support/resistance levels from swing highs/lows + classic pivots."""
    highs   = df["High"].values.astype(float)
    lows    = df["Low"].values.astype(float)
    current = float(df["Close"].iloc[-1])

    raw: list = []   # (price, weight)

    # Swing highs and lows (look-left / look-right window)
    for i in range(n_swing, len(df) - n_swing):
        h, l = highs[i], lows[i]
        if h >= max(highs[i - n_swing: i]) and h >= max(highs[i + 1: i + n_swing + 1]):
            raw.append((h, 1))
        if l <= min(lows[i - n_swing: i]) and l <= min(lows[i + 1: i + n_swing + 1]):
            raw.append((l, 1))

    # Classic pivot points from the last 20 sessions
    tail = df.tail(20)
    ph = float(tail["High"].max())
    pl = float(tail["Low"].min())
    pp = (ph + pl + current) / 3
    rng = ph - pl
    if rng > 0:
        raw += [(pp, 2), (2*pp - pl, 2), (pp + rng, 2), (2*pp - ph, 2), (pp - rng, 2)]

    if not raw:
        return []

    # Cluster levels within cluster_pct band
    raw.sort(key=lambda x: x[0])
    clustered: list = []
    i = 0
    while i < len(raw):
        grp = [raw[i]]
        j = i + 1
        while j < len(raw) and abs(raw[j][0] - raw[i][0]) / raw[i][0] < cluster_pct:
            grp.append(raw[j])
            j += 1
        avg_p    = sum(g[0] for g in grp) / len(grp)
        strength = sum(g[1] for g in grp)
        clustered.append({
            "price":    round(avg_p, 2),
            "strength": strength,
            "type":     "resistance" if avg_p > current else "support",
        })
        i = j

    supports    = sorted([l for l in clustered if l["type"] == "support"],    key=lambda x: -x["price"])[:max_each]
    resistances = sorted([l for l in clustered if l["type"] == "resistance"], key=lambda x:  x["price"])[:max_each]
    return supports + resistances


@app.route("/api/chart/<symbol>")
def api_chart(symbol):
    symbol = symbol.upper()
    try:
        import math
        from src.data.fetcher import MarketDataFetcher
        fetcher = MarketDataFetcher(lookback_days=90, interval="1d")
        df = fetcher.fetch(symbol)
        if df is None or df.empty:
            return jsonify({"ok": False, "error": f"No data for {symbol}"}), 404
        if df.index.tz is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        close  = df["Close"]
        volume = df["Volume"]
        ema_f = close.ewm(span=config.ema_fast, adjust=False).mean()
        ema_s = close.ewm(span=config.ema_slow, adjust=False).mean()
        bb_mid = close.rolling(config.bb_period).mean()
        bb_std_ser = close.rolling(config.bb_period).std()
        bb_upper = bb_mid + config.bb_std * bb_std_ser
        bb_lower = bb_mid - config.bb_std * bb_std_ser
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta.clip(upper=0))
        avg_gain = gain.ewm(com=config.rsi_period - 1, min_periods=config.rsi_period).mean()
        avg_loss = loss.ewm(com=config.rsi_period - 1, min_periods=config.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi_ser = 100 - (100 / (1 + rs))
        avg_vol = volume.rolling(20).mean()
        vol_ratio = (volume / avg_vol.replace(0, float("nan"))).fillna(1.0)
        def to_list(s):
            return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else round(float(v), 4) for v in s]
        sr_levels = _compute_sr_levels(df)
        return jsonify({
            "ok": True,
            "symbol": symbol,
            "ema_fast_period": config.ema_fast,
            "ema_slow_period": config.ema_slow,
            "dates":      df.index.strftime("%Y-%m-%d").tolist(),
            "open":       to_list(df["Open"]),
            "high":       to_list(df["High"]),
            "low":        to_list(df["Low"]),
            "close":      to_list(close),
            "volume":     [int(v) if v is not None else None for v in to_list(volume)],
            "vol_ratio":  to_list(vol_ratio),
            "ema_fast":   to_list(ema_f),
            "ema_slow":   to_list(ema_s),
            "bb_upper":   to_list(bb_upper),
            "bb_middle":  to_list(bb_mid),
            "bb_lower":   to_list(bb_lower),
            "rsi":        to_list(rsi_ser),
            "sr_levels":  sr_levels,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/journal")
def api_journal():
    try:
        eng = _get_engine()
        entries = list(reversed(eng.journal.read_recent(200)))
        return jsonify({"ok": True, "entries": entries, "stats": eng.journal.stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/leaderboard")
def api_leaderboard():
    try:
        entries = _get_engine().journal.read_all()
        sells = [e for e in entries if e.get("action") == "SELL" and e.get("pnl_pct") is not None]
        buys  = [e for e in entries if e.get("action") == "BUY"]

        if not sells:
            return jsonify({"ok": True, "stats": {"total_trades": 0}, "trades": [], "chart": []})

        pnl_pcts = [e["pnl_pct"] for e in sells]
        winners  = [p for p in pnl_pcts if p > 0]

        # Build buy-timestamp index for holding period
        buys_by_sym: dict = {}
        for b in buys:
            buys_by_sym.setdefault(b["symbol"], []).append(b["timestamp"])

        def _hold_days(sell_entry):
            sym = sell_entry["symbol"]
            sell_ts = sell_entry["timestamp"]
            prior = [ts for ts in buys_by_sym.get(sym, []) if ts <= sell_ts]
            if not prior:
                return None
            buy_dt  = datetime.fromisoformat(max(prior))
            sell_dt = datetime.fromisoformat(sell_ts)
            return max(0, (sell_dt - buy_dt).days)

        hold_days_list = [d for d in (_hold_days(s) for s in sells) if d is not None]
        avg_hold = round(sum(hold_days_list) / len(hold_days_list), 0) if hold_days_list else None

        best_idx  = pnl_pcts.index(max(pnl_pcts))
        worst_idx = pnl_pcts.index(min(pnl_pcts))

        stats = {
            "total_trades":    len(sells),
            "winners":         len(winners),
            "win_rate":        round(len(winners) / len(sells) * 100, 1),
            "total_return_pct": round(sum(pnl_pcts) * 100, 2),
            "best_trade_pct":  round(max(pnl_pcts) * 100, 2),
            "best_symbol":     sells[best_idx]["symbol"],
            "worst_trade_pct": round(min(pnl_pcts) * 100, 2),
            "worst_symbol":    sells[worst_idx]["symbol"],
            "avg_hold_days":   int(avg_hold) if avg_hold is not None else None,
        }

        # Cumulative returns chart points
        chart, cumulative = [], 0.0
        for e in sells:
            cumulative += e["pnl_pct"] * 100
            chart.append({"ts": e["timestamp"][:10], "value": round(cumulative, 2)})

        # Last 20 trades table
        last20 = list(reversed(sells[-20:]))
        trades = []
        for e in last20:
            exit_px  = e["price"]
            pp       = e["pnl_pct"]
            entry_px = round(exit_px / (1 + pp), 2) if pp != -1 else None
            trades.append({
                "date":        e["timestamp"][:10],
                "symbol":      e["symbol"],
                "exit_price":  round(exit_px, 2),
                "entry_price": entry_px,
                "pnl_pct":     round(pp * 100, 2),
                "hold_days":   _hold_days(e),
            })

        return jsonify({"ok": True, "stats": stats, "trades": trades, "chart": chart})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    global _current_profile
    if request.method == "GET":
        return jsonify({
            "ok": True,
            "risk_profile": _current_profile,
            "email_notifications": _get_engine().emailer.active,
            "email_configured": _get_engine().emailer.is_configured,
            "profiles": {
                k: {"label": v["label"], "tagline": v["tagline"], "color": v["color"],
                    "score_label": v.get("score_label", ""), "overrides": v["overrides"]}
                for k, v in RISK_PROFILES.items()
            },
        })
    data = request.get_json(silent=True) or {}
    saved = _load_user_settings()
    # Risk profile update
    name = data.get("risk_profile", "")
    if name:
        if not _apply_risk_profile(name):
            return jsonify({"ok": False, "error": f"Unknown profile: {name}"}), 400
        saved["risk_profile"] = name
    # Email notifications toggle
    if "email_notifications" in data:
        enabled = bool(data["email_notifications"])
        if enabled and not _get_engine().emailer.is_configured:
            return jsonify({"ok": False, "error": "Email not configured — set EMAIL_HOST, EMAIL_USER, EMAIL_PASSWORD, NOTIFY_EMAIL environment variables."}), 400
        _get_engine().emailer.active = enabled
        saved["email_notifications"] = enabled
    _save_user_settings(saved)
    return jsonify({
        "ok": True,
        "risk_profile": _current_profile,
        "email_notifications": _get_engine().emailer.active,
    })


@app.route("/api/alpaca-keys", methods=["POST"])
def api_alpaca_keys():
    """Save per-user Alpaca credentials (encrypted at rest)."""
    if not _AUTH_ENABLED:
        return jsonify({"ok": False, "error": "Auth not enabled"}), 400
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    api_key    = (data.get("api_key") or "").strip()
    secret_key = (data.get("secret_key") or "").strip()
    paper      = bool(data.get("paper", True))
    if not api_key or not secret_key:
        return jsonify({"ok": False, "error": "api_key and secret_key are required"}), 400
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"ok": False, "error": "User not found"}), 404
    user.alpaca_api_key_enc    = _encrypt_key(api_key)
    user.alpaca_secret_key_enc = _encrypt_key(secret_key)
    user.alpaca_paper          = paper
    db.session.commit()
    _invalidate_user_engine(user_id)
    return jsonify({"ok": True})


@app.route("/api/news")
def api_news():
    """Return recent headlines for watchlist symbols (15-min cache, ≤8 symbols)."""
    import yfinance as yf
    watchlist = (_get_engine().watchlist or [])[:8]
    if not watchlist:
        return jsonify({"ok": True, "items": []})

    now      = datetime.now()
    all_items: list = []

    for sym in watchlist:
        cached = _news_cache.get(sym)
        if cached and (now - cached["fetched_at"]).total_seconds() < _NEWS_CACHE_TTL:
            all_items.extend(cached["items"])
            continue
        try:
            raw   = yf.Ticker(sym).news or []
            items = [_parse_news_item(n, sym) for n in raw[:5] if n]
            items = [it for it in items if it.get("title")]
            _news_cache[sym] = {"items": items, "fetched_at": now}
            all_items.extend(items)
        except Exception as e:
            log.debug(f"News fetch {sym}: {e}")

    all_items.sort(key=lambda x: x.get("published_at") or 0, reverse=True)
    return jsonify({"ok": True, "items": all_items[:30]})


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    if _backtest_running:
        return jsonify({"ok": False, "reason": "backtest already running"})
    threading.Thread(target=_run_backtest_bg, daemon=True, name="backtest-manual").start()
    return jsonify({"ok": True, "message": "Backtest started — poll /api/backtest/report"})


@app.route("/api/backtest/report")
def api_backtest_report():
    if _backtest_report:
        return jsonify({"ok": True, "report": _backtest_report, "running": _backtest_running})
    if _BACKTEST_REPORT_PATH.exists():
        try:
            with open(_BACKTEST_REPORT_PATH) as f:
                return jsonify({"ok": True, "report": json.load(f), "running": _backtest_running})
        except Exception:
            pass
    return jsonify({"ok": False, "running": _backtest_running, "reason": "no report yet"})


# ── Stock search & personal watchlist endpoints ───────────────────────────────

@app.route("/api/search/<symbol>")
def api_search(symbol):
    import re
    symbol = symbol.upper().strip()
    if not symbol or not re.match(r"^[A-Z0-9.\-]{1,6}$", symbol):
        return jsonify({"ok": False, "error": f"Invalid symbol: {symbol}"}), 400
    try:
        import yfinance as yf
        from src.data.fetcher import MarketDataFetcher
        from src.signals.analyzer import SignalAnalyzer
        from src.signals.indicators import TechnicalIndicators

        fetcher = MarketDataFetcher(lookback_days=60, interval="1d")
        df = fetcher.fetch(symbol)

        rsi = score = action = None
        if df is not None and not df.empty:
            ind_calc = TechnicalIndicators(
                rsi_period=config.rsi_period, macd_fast=config.macd_fast,
                macd_slow=config.macd_slow, macd_signal=config.macd_signal,
                ema_fast=config.ema_fast, ema_slow=config.ema_slow,
                bb_period=config.bb_period, bb_std=config.bb_std,
            )
            ind = ind_calc.compute(df)
            sig = SignalAnalyzer(
                buy_threshold=config.buy_threshold,
                sell_threshold=config.sell_threshold,
            ).analyze(ind)
            rsi      = round(ind.rsi, 1) if ind.rsi is not None else None
            roc_10   = round(ind.roc_10 * 100, 2) if getattr(ind, "roc_10", None) is not None else None
            stoch_rsi = round(ind.stoch_rsi, 1) if getattr(ind, "stoch_rsi", None) is not None else None
            score    = round(sig.score, 3)
            action   = sig.action

        info = {}
        price = None
        try:
            info  = yf.Ticker(symbol).info or {}
            price = (info.get("regularMarketPrice") or info.get("currentPrice")
                     or info.get("previousClose"))
        except Exception:
            pass
        if price is None and df is not None and not df.empty:
            price = float(df["Close"].iloc[-1])

        return jsonify({
            "ok":        True,
            "symbol":    symbol,
            "name":      info.get("longName") or info.get("shortName") or symbol,
            "price":     round(float(price), 2) if price else None,
            "sector":    info.get("sector") or get_sector(symbol) or "—",
            "pe_ratio":  round(float(info["trailingPE"]), 1) if info.get("trailingPE") else None,
            "market_cap": info.get("marketCap"),
            "rsi":       rsi,
            "roc_10":    roc_10,
            "stoch_rsi": stoch_rsi,
            "score":     score,
            "action":    action,
            "pinned":    symbol in _personal_watchlist,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/watchlist")
def api_watchlist_get():
    if not _personal_watchlist:
        return jsonify({"ok": True, "symbols": [], "items": []})
    items = []
    # Use engine's fetcher cache (no extra network calls)
    try:
        market_data = _get_engine().fetcher.fetch_many(_personal_watchlist, force_refresh=False)
    except Exception:
        market_data = {}
    # Last computed signals for signal/rsi columns
    with _lock:
        sig_lookup = {s["symbol"]: s for s in _last_state.get("signals", [])}

    for sym in _personal_watchlist:
        price = change_pct = rsi = score = action = None
        df = market_data.get(sym)
        if df is not None and not df.empty:
            price = round(float(df["Close"].iloc[-1]), 2)
            if len(df) >= 2:
                prev = float(df["Close"].iloc[-2])
                change_pct = round((price / prev - 1) * 100, 2) if prev else None
        sig = sig_lookup.get(sym)
        if sig:
            rsi    = sig.get("rsi")
            score  = sig.get("score")
            action = sig.get("action")
        items.append({"symbol": sym, "price": price, "change_pct": change_pct,
                       "rsi": rsi, "score": score, "action": action or "—"})
    return jsonify({"ok": True, "symbols": _personal_watchlist, "items": items})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    sym = (request.json or {}).get("symbol", "").upper().strip()
    if not sym:
        return jsonify({"ok": False, "error": "No symbol"}), 400
    if sym not in _personal_watchlist:
        _personal_watchlist.append(sym)
        _save_personal_watchlist()
    return jsonify({"ok": True, "symbols": _personal_watchlist})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    sym = (request.json or {}).get("symbol", "").upper().strip()
    if sym in _personal_watchlist:
        _personal_watchlist.remove(sym)
        _save_personal_watchlist()
    return jsonify({"ok": True, "symbols": _personal_watchlist})


# ── PWA routes ────────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def pwa_manifest():
    from flask import Response
    m = {
        "name":             "NYSE Trading Engine",
        "short_name":       "TradingEng",
        "start_url":        "/",
        "display":          "standalone",
        "background_color": "#0f172a",
        "theme_color":      "#1e293b",
        "description":      "Algorithmic NYSE trading engine dashboard",
        "icons": [
            {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ],
    }
    return Response(json.dumps(m), mimetype="application/manifest+json")


@app.route("/icon-<size>.svg")
def pwa_icon(size):
    from flask import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="22" fill="#1e293b"/>'
        '<polyline points="10,80 28,50 48,62 68,28 90,18" fill="none" '
        'stroke="#22c55e" stroke-width="9" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


@app.route("/sw.js")
def service_worker():
    from flask import Response
    # nyse-v2: removed HTML caching — pages must always come from the network so
    # auth redirects work correctly. Old 'nyse-v1' cache is deleted on activate.
    js = """
const CACHE = 'nyse-v2';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', e => {
  // Network-only for HTML pages so auth redirects always work
  if (e.request.mode === 'navigate') return;
  // Network with offline fallback for API calls
  if (e.request.url.includes('/api/')) {
    e.respondWith(fetch(e.request).catch(() =>
      new Response('{}', {headers: {'Content-Type': 'application/json'}})
    ));
  }
});
""".strip()
    return Response(js, mimetype="application/javascript")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="theme-color" content="#1e293b"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"/>
<link rel="manifest" href="/manifest.json"/>
<link rel="apple-touch-icon" href="/icon-192.svg"/>
<title>NYSE Trading Engine</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
a{color:inherit;text-decoration:none}

/* ── Header ──────────────────────────────────────────────────────────────── */
header{background:#1e293b;border-bottom:1px solid #334155;padding:12px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10;flex-wrap:wrap}
.logo{font-size:17px;font-weight:700;color:#f1f5f9;letter-spacing:.5px;white-space:nowrap}
.badge{padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.badge-paper{background:#1d4ed8;color:#bfdbfe}
.badge-live{background:#7f1d1d;color:#fecaca}
.badge-sim{background:#374151;color:#9ca3af}
.market-dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px;flex-shrink:0}
.market-open{background:#22c55e;box-shadow:0 0 6px #22c55e}
.market-closed{background:#ef4444}
.market-unknown{background:#6b7280}
#market-status{font-size:12px;color:#94a3b8;white-space:nowrap}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.ts{font-size:11px;color:#64748b;white-space:nowrap}

/* ── Buttons ─────────────────────────────────────────────────────────────── */
button{cursor:pointer;padding:7px 16px;border-radius:6px;border:none;font-size:13px;font-weight:600;transition:opacity .15s;min-height:36px;touch-action:manipulation}
.btn-refresh{background:#334155;color:#e2e8f0}
.btn-refresh:hover{opacity:.8}
.btn-rescan{background:#7c3aed;color:#fff}
.btn-rescan:hover{opacity:.85}
.btn-rescan:disabled,.btn-cycle:disabled,.btn-voo:disabled{opacity:.5;cursor:not-allowed}
.btn-cycle{background:#0ea5e9;color:#fff}
.btn-cycle:hover{opacity:.85}

/* ── Layout ──────────────────────────────────────────────────────────────── */
main{padding:16px 20px;max-width:1400px;margin:0 auto}

/* ── Stat cards ──────────────────────────────────────────────────────────── */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:16px}
.card{background:#1e293b;border-radius:10px;padding:14px 16px;border:1px solid #334155}
.card-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card-value{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.card-sub{font-size:11px;color:#64748b;margin-top:3px}
.pos{color:#22c55e}.neg{color:#ef4444}.neu{color:#e2e8f0}

/* ── Grid ────────────────────────────────────────────────────────────────── */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}
.grid1{margin-bottom:14px}

/* ── Panels ──────────────────────────────────────────────────────────────── */
.panel{background:#1e293b;border-radius:10px;border:1px solid #334155;overflow:hidden}
.panel-title{padding:11px 14px;font-weight:600;font-size:12px;color:#94a3b8;border-bottom:1px solid #334155;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.panel-title .count{background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px}

/* ── Table scroll wrapper ────────────────────────────────────────────────── */
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}

/* ── Tables ──────────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse;min-width:340px}
th{padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #334155;white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid #1e293b;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#263044}

/* ── Signal pill ─────────────────────────────────────────────────────────── */
.pill{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}
.pill-BUY{background:#14532d;color:#4ade80}
.pill-SELL{background:#7f1d1d;color:#f87171}
.pill-HOLD{background:#374151;color:#9ca3af}

/* ── Score bar ───────────────────────────────────────────────────────────── */
.score-wrap{display:flex;align-items:center;gap:6px}
.score-bar-bg{width:52px;height:5px;background:#334155;border-radius:3px;overflow:hidden;flex-shrink:0}
.score-bar{height:5px;border-radius:3px;transition:width .3s}

/* ── VOO monitor ─────────────────────────────────────────────────────────── */
.voo-panel{background:#1e293b;border-radius:10px;border:1px solid #334155;overflow:hidden;margin-bottom:14px}
.voo-header{padding:11px 14px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.voo-title{font-weight:600;font-size:12px;color:#94a3b8;text-transform:uppercase;letter-spacing:.5px}
.voo-checked{font-size:11px;color:#475569;margin-left:auto}
.voo-stats{display:grid;grid-template-columns:repeat(3,1fr)}
.voo-stat{padding:16px 18px;border-right:1px solid #334155}
.voo-stat:last-child{border-right:none}
.voo-stat-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.voo-stat-value{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.voo-alert-bar{padding:13px 18px;display:flex;align-items:center;gap:10px;font-size:13px;font-weight:600}
.voo-above{background:#0f2318;color:#4ade80;border-top:1px solid #166534}
.voo-below{background:#14532d;color:#dcfce7;border-top:1px solid #22c55e;animation:voo-pulse 2s ease-in-out infinite}
.voo-loading{padding:22px;text-align:center;color:#475569;font-size:13px}
@keyframes voo-pulse{0%,100%{opacity:1}50%{opacity:.8}}
.btn-voo{background:#1d4ed8;color:#fff;font-size:12px;padding:5px 12px}
.btn-voo:hover{opacity:.85}

/* ── Sector exposure strip ───────────────────────────────────────────────── */
.sector-strip{display:flex;flex-wrap:wrap;gap:6px;padding:10px 14px;border-bottom:1px solid #334155}
.sector-chip{padding:3px 10px;border-radius:99px;font-size:11px;font-weight:600;background:#1e3a5f;color:#93c5fd;border:1px solid #1d4ed8}
.sector-chip.near-limit{background:#451a03;color:#fdba74;border-color:#92400e}
.sector-chip.at-limit{background:#7f1d1d;color:#fca5a5;border-color:#b91c1c}

/* ── Regime card colours ─────────────────────────────────────────────────── */
.regime-bull{color:#22c55e}
.regime-bear{color:#ef4444}
.regime-choppy{color:#f59e0b}

/* ── MTF sub-scores ──────────────────────────────────────────────────────── */
.mtf-scores{font-size:10px;color:#64748b;margin-top:2px;letter-spacing:.2px}
.mtf-scores span{margin-right:5px;white-space:nowrap}

/* ── Sub-lines inside signal table cells ─────────────────────────────────── */
.sig-sub{font-size:10px;color:#475569;margin-top:2px}
.sig-sub span{margin-right:5px;white-space:nowrap}

/* ── Misc ────────────────────────────────────────────────────────────────── */
.empty{padding:28px;text-align:center;color:#475569}
.error-banner{background:#7f1d1d;color:#fecaca;border-radius:8px;padding:10px 16px;margin-bottom:14px;font-size:13px;display:none}
.spinner{display:inline-block;width:14px;height:14px;border:2px solid #334155;border-top-color:#0ea5e9;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-overlay{display:none;position:fixed;inset:0;background:rgba(15,23,42,.7);z-index:50;align-items:center;justify-content:center;flex-direction:column;gap:12px}
.loading-overlay.active{display:flex}

/* ── Chart modal ─────────────────────────────────────────────────────────── */
.chart-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:200;overflow:auto;padding:16px}
.chart-modal.active{display:block}
.chart-box{max-width:1120px;margin:0 auto;background:#1e293b;border-radius:12px;border:1px solid #334155;overflow:hidden}
.chart-hdr{padding:12px 16px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #334155}
.chart-sym{font-weight:700;font-size:16px;color:#f1f5f9}
.chart-meta{font-size:12px;color:#64748b}
.chart-close{margin-left:auto;background:#334155;color:#e2e8f0;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
.chart-close:hover{opacity:.8}
.chart-body{padding:8px;background:#0f172a}
.sym-link{cursor:pointer;color:#93c5fd}
.sym-link:hover{text-decoration:underline}

/* ── Responsive — tablet (≤ 900 px) ─────────────────────────────────────── */
@media(max-width:900px){
  .grid2{grid-template-columns:1fr}
}

/* ── Responsive — phone (≤ 600 px) ──────────────────────────────────────── */
@media(max-width:600px){
  .vol-col,.z-col{display:none}

  header{padding:10px 14px;gap:8px}
  .logo{font-size:15px}
  .ts{display:none}
  .hdr-right{width:100%;margin-left:0;justify-content:flex-end}
  .btn-refresh,.btn-rescan,.btn-cycle{padding:8px 12px;font-size:12px;flex:1;text-align:center}

  main{padding:10px 12px}

  .cards{grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
  .card{padding:11px 12px}
  .card-value{font-size:18px}
  .card-label{font-size:10px}

  .voo-stats{grid-template-columns:1fr}
  .voo-stat{border-right:none;border-bottom:1px solid #334155;padding:12px 14px}
  .voo-stat:last-child{border-bottom:none}
  .voo-stat-value{font-size:20px}
  .voo-header{gap:6px}
  .voo-checked{width:100%;margin-left:0;font-size:10px}
  .btn-voo{width:100%;margin-top:4px}

  .panel-title{font-size:11px;padding:10px 12px}
  th{padding:7px 8px;font-size:10px}
  td{padding:8px 8px;font-size:12px}
  table{min-width:300px}

  .score-bar-bg{width:36px}
  .pill{font-size:10px;padding:2px 6px}
}

/* ── Sector pie chart panel ──────────────────────────────────────────────── */
.sector-chart-wrap{padding:4px 8px 8px}

/* ── Watchlist heat map ──────────────────────────────────────────────────── */
.hm-grid{display:flex;flex-wrap:wrap;gap:6px;padding:12px 14px}
.hm-cell{border-radius:8px;padding:8px 10px;min-width:80px;flex:1 1 80px;max-width:130px;cursor:default;transition:transform .1s,opacity .1s;border:1px solid rgba(255,255,255,0.06)}
.hm-cell:hover{transform:scale(1.04);opacity:.9}
.hm-sym{font-weight:700;font-size:13px;letter-spacing:.3px;color:#f1f5f9}
.hm-pct{font-size:12px;font-weight:600;margin-top:1px;font-variant-numeric:tabular-nums}
.hm-price{font-size:10px;color:rgba(255,255,255,0.45);margin-top:2px;font-variant-numeric:tabular-nums}
body.light .hm-sym{color:#0f172a}
body.light .hm-cell{border-color:rgba(0,0,0,0.08)}
body.light .hm-price{color:rgba(0,0,0,0.4)}

/* ── Period P&L tab buttons ──────────────────────────────────────────────── */
.tab-btns{display:flex;gap:6px}
.tab-btn{padding:3px 12px;border-radius:99px;border:1px solid #334155;background:none;color:#64748b;font-size:11px;font-weight:600;cursor:pointer;min-height:24px}
.tab-btn.active{background:#334155;color:#e2e8f0;border-color:#334155}
body.light .tab-btn{border-color:#e2e8f0;color:#64748b}
body.light .tab-btn.active{background:#e2e8f0;color:#1e293b;border-color:#e2e8f0}

/* ── Light theme ─────────────────────────────────────────────────────────────
   Additive overrides — every dark colour is re-declared here so the rest of
   the CSS never needs to be touched when the theme changes.
   ─────────────────────────────────────────────────────────────────────────── */
body.light{background:#f1f5f9;color:#1e293b}
body.light header{background:#fff;border-bottom-color:#e2e8f0}
body.light .logo{color:#0f172a}
body.light .badge-sim{background:#e2e8f0;color:#475569}
body.light #market-status{color:#475569}
body.light #cycle-info,.ts{color:#94a3b8}
body.light .card{background:#fff;border-color:#e2e8f0}
body.light .card-label{color:#64748b}
body.light .card-sub{color:#94a3b8}
body.light .panel{background:#fff;border-color:#e2e8f0}
body.light .panel-title{color:#475569;border-bottom-color:#e2e8f0}
body.light .panel-title .count{background:#e2e8f0;color:#475569}
body.light th{color:#475569;border-bottom-color:#e2e8f0}
body.light td{border-bottom-color:#f1f5f9}
body.light tr:hover td{background:#f8fafc}
body.light .btn-refresh{background:#e2e8f0;color:#1e293b}
body.light .score-bar-bg{background:#e2e8f0}
body.light .pill-HOLD{background:#e2e8f0;color:#475569}
body.light .spinner{border-color:#e2e8f0}
body.light .loading-overlay{background:rgba(241,245,249,.85)}
body.light .error-banner{background:#fee2e2;color:#991b1b}
body.light .empty{color:#94a3b8}
body.light .mtf-scores{color:#94a3b8}
body.light .sig-sub{color:#94a3b8}
body.light .sym-link{color:#1d4ed8}
body.light .voo-panel{background:#fff;border-color:#e2e8f0}
body.light .voo-header{background:#fff;border-bottom-color:#e2e8f0}
body.light .voo-title{color:#475569}
body.light .voo-stat{border-right-color:#e2e8f0}
body.light .voo-stat-label{color:#64748b}
body.light .voo-loading{color:#94a3b8}
body.light .voo-checked{color:#94a3b8}
body.light .voo-above{background:#f0fdf4;color:#166534;border-top-color:#bbf7d0}
body.light .voo-below{background:#dcfce7;color:#14532d;border-top-color:#86efac}
body.light .chart-box{background:#fff;border-color:#e2e8f0}
body.light .chart-hdr{border-bottom-color:#e2e8f0}
body.light .chart-sym{color:#0f172a}
body.light .chart-close{background:#e2e8f0;color:#1e293b}
body.light .chart-body{background:#f8fafc}
body.light .sector-chip{background:#dbeafe;color:#1d4ed8;border-color:#93c5fd}
body.light .sector-chip.near-limit{background:#fff7ed;color:#c2410c;border-color:#fdba74}
body.light .sector-chip.at-limit{background:#fee2e2;color:#b91c1c;border-color:#fca5a5}

/* ── Theme toggle button ──────────────────────────────────────────────────── */
.theme-toggle{background:none;border:1px solid #334155;color:#e2e8f0;padding:0;border-radius:99px;font-size:16px;min-height:32px;width:36px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
body.light .theme-toggle{border-color:#cbd5e1;color:#1e293b}

/* ── Live P&L ticker (header) ─────────────────────────────────────────────── */
.pnl-ticker{display:flex;flex-direction:column;align-items:flex-end;font-variant-numeric:tabular-nums;white-space:nowrap;line-height:1.25;padding:0 4px;border-left:1px solid #334155;margin-left:4px}
body.light .pnl-ticker{border-left-color:#e2e8f0}
.pnl-ticker-label{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.4px}
.pnl-ticker-value{font-size:15px;font-weight:700}
.pnl-ticker-pct{font-size:11px;font-weight:600}
@keyframes pnl-flash{0%{opacity:1}35%{opacity:.25}100%{opacity:1}}
.pnl-flash{animation:pnl-flash .55s ease}
@media(max-width:600px){.pnl-ticker{display:none}}

/* ── Stock search panel ──────────────────────────────────────────────────────── */
.search-bar{display:flex;gap:8px;padding:12px 14px;border-bottom:1px solid #334155;flex-wrap:wrap}
.search-bar input{flex:1;min-width:120px;background:#0f172a;border:1px solid #334155;border-radius:6px;padding:8px 12px;color:#e2e8f0;font-size:14px}
.search-bar input:focus{outline:none;border-color:#0ea5e9}
.search-bar input::placeholder{color:#475569}
.btn-search{background:#0ea5e9;color:#fff;padding:8px 20px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn-search:hover{opacity:.85}
.search-result{padding:14px 16px}
.sr-header{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.sr-name{font-size:15px;font-weight:700;color:#f1f5f9}
.sr-company{font-size:12px;color:#64748b}
.sr-stats{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:12px}
.sr-stat{display:flex;flex-direction:column;gap:2px}
.sr-stat-label{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.4px}
.sr-stat-value{font-size:16px;font-weight:700;font-variant-numeric:tabular-nums;color:#e2e8f0}
.btn-pin{padding:7px 16px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer}
.btn-pin-add{background:#14532d;color:#4ade80}.btn-pin-add:hover{opacity:.85}
.btn-pin-rem{background:#7f1d1d;color:#f87171}.btn-pin-rem:hover{opacity:.85}
/* ── Pinned watchlist cards ──────────────────────────────────────────────────── */
.pin-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;padding:12px 14px}
.pin-card{background:#0f172a;border-radius:8px;padding:10px 12px;border:1px solid #334155;position:relative;min-width:0}
.pin-sym{font-size:15px;font-weight:700;color:#f1f5f9;margin-bottom:4px;padding-right:18px}
.pin-price{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums;color:#e2e8f0}
.pin-change{font-size:11px;font-weight:600;margin-top:1px;font-variant-numeric:tabular-nums}
.pin-rsi{font-size:11px;color:#64748b;margin-top:3px}
.pin-remove{position:absolute;top:6px;right:8px;background:none;border:none;color:#475569;font-size:14px;cursor:pointer;padding:0;min-height:unset;line-height:1}
.pin-remove:hover{color:#f87171}
/* ── Public URL badge ─────────────────────────────────────────────────────────── */
.public-url-wrap{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:6px;background:#0f2318;border:1px solid #166534;white-space:nowrap;flex-shrink:0}
.public-url-label{font-size:10px;color:#4ade80;font-weight:600;text-transform:uppercase;letter-spacing:.4px}
.public-url-val{font-size:11px;color:#4ade80;font-weight:600;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.btn-copy-url{background:none;border:none;color:#4ade80;cursor:pointer;font-size:11px;padding:1px 4px;min-height:unset}
.btn-copy-url:hover{opacity:.7}
@media(max-width:900px){.public-url-val{max-width:120px}}
@media(max-width:600px){.public-url-wrap{display:none}}
/* light theme for new panels */
body.light .search-bar{border-bottom-color:#e2e8f0}
body.light .search-bar input{background:#f8fafc;border-color:#e2e8f0;color:#1e293b}
body.light .search-bar input::placeholder{color:#94a3b8}
body.light .sr-name{color:#0f172a}
body.light .sr-stat-value{color:#1e293b}
body.light .pin-card{background:#f8fafc;border-color:#e2e8f0}
body.light .pin-sym,.pin-price{color:#0f172a}
body.light .pin-remove{color:#94a3b8}
body.light .public-url-wrap{background:#f0fdf4;border-color:#bbf7d0}
body.light .public-url-label,.public-url-val{color:#166534}
/* ── News feed panel ─────────────────────────────────────────────────────────── */
.news-item{display:flex;flex-direction:column;gap:2px;padding:10px 14px;border-bottom:1px solid #1e293b;transition:background .12s}
.news-item:last-child{border-bottom:none}
.news-item:hover{background:#263044}
.news-sym{display:inline-block;padding:1px 7px;border-radius:99px;font-size:10px;font-weight:700;background:#1e3a5f;color:#93c5fd;margin-right:6px;flex-shrink:0}
.news-title{font-size:13px;color:#e2e8f0;line-height:1.4;cursor:pointer}
.news-title:hover{color:#93c5fd;text-decoration:underline}
.news-meta{font-size:10px;color:#475569;margin-top:1px}
.news-loading{padding:22px;text-align:center;color:#475569;font-size:13px}
body.light .news-item{border-bottom-color:#f1f5f9}
body.light .news-item:hover{background:#f8fafc}
body.light .news-sym{background:#dbeafe;color:#1d4ed8}
body.light .news-title{color:#1e293b}
body.light .news-title:hover{color:#1d4ed8}
body.light .news-meta{color:#94a3b8}

/* ── Explain Trade modal ─────────────────────────────────────────────────── */
.explain-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);
               z-index:300;padding:20px;align-items:center;justify-content:center}
.explain-modal.active{display:flex}
.explain-box{width:100%;max-width:560px;background:#0f1629;border-radius:14px;
             border:1px solid #1e2d45;overflow:hidden;box-shadow:0 24px 64px rgba(0,0,0,.7)}
.explain-hdr{padding:16px 20px;display:flex;align-items:center;justify-content:space-between;
             border-bottom:1px solid #1e2d45;background:#141d2e;gap:10px}
.explain-sym{font-weight:800;font-size:20px;color:#f1f5f9;letter-spacing:-.5px}
.explain-close{background:none;border:1px solid #1e2d45;color:#8898b8;padding:5px 12px;
               border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;
               min-height:unset;transition:border-color .15s}
.explain-close:hover{border-color:#3b82f6;color:#e2e8f0}
.explain-body{padding:20px;max-height:70vh;overflow-y:auto}
.explain-score{font-size:13px;color:#8898b8;margin-bottom:16px;padding-bottom:14px;
               border-bottom:1px solid #1e2d45}
.explain-score strong{color:#f1f5f9;font-size:18px;font-weight:800}
.explain-item{display:flex;gap:12px;margin-bottom:14px;align-items:flex-start}
.explain-item:last-child{margin-bottom:0}
.explain-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;
              justify-content:center;font-size:15px;flex-shrink:0;margin-top:1px}
.ei-bull{background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.2)}
.ei-bear{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.2)}
.ei-neu{background:rgba(100,116,139,.15);border:1px solid rgba(100,116,139,.2)}
.explain-text{flex:1}
.explain-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;
               margin-bottom:3px}
.el-bull{color:#34d399}.el-bear{color:#f87171}.el-neu{color:#94a3b8}
.explain-detail{font-size:13px;color:#c8d4e8;line-height:1.55}
.explain-val{font-weight:700;color:#f1f5f9}
.explain-reasons{margin-top:16px;padding-top:14px;border-top:1px solid #1e2d45}
.explain-reasons-title{font-size:11px;font-weight:700;text-transform:uppercase;
                        letter-spacing:.5px;color:#4d6380;margin-bottom:8px}
.explain-reason{font-size:12px;color:#8898b8;padding:5px 0;border-bottom:1px solid rgba(30,45,69,.5);
                line-height:1.45}
.explain-reason:last-child{border-bottom:none}
body.light .explain-box{background:#fff;border-color:#e2e8f0}
body.light .explain-hdr{background:#f8fafc;border-bottom-color:#e2e8f0}
body.light .explain-sym{color:#0f172a}
body.light .explain-score{border-bottom-color:#e2e8f0;color:#475569}
body.light .explain-score strong{color:#1e293b}
body.light .explain-detail{color:#374151}
body.light .explain-val{color:#0f172a}
body.light .explain-reasons{border-top-color:#e2e8f0}
body.light .explain-reason{color:#64748b;border-bottom-color:#f1f5f9}
body.light .explain-close{border-color:#e2e8f0;color:#64748b}
</style>
</head>
<body>

<!-- Chart modal — click any ticker in Signal Analysis to open -->
<div class="chart-modal" id="chart-modal" onclick="if(event.target===this)closeChart()">
  <div class="chart-box">
    <div class="chart-hdr">
      <span class="chart-sym" id="chart-sym">—</span>
      <span class="chart-meta" id="chart-meta">90-day daily</span>
      <button class="chart-close" onclick="closeChart()">✕ Close</button>
    </div>
    <div class="chart-body">
      <div id="chart-plotly" style="height:540px"></div>
    </div>
  </div>
</div>

<!-- Explain Trade modal -->
<div class="explain-modal" id="explain-modal" onclick="if(event.target===this)closeExplain()">
  <div class="explain-box">
    <div class="explain-hdr">
      <div style="display:flex;align-items:center;gap:10px">
        <span class="explain-sym" id="explain-sym">—</span>
        <span class="pill" id="explain-pill">—</span>
      </div>
      <button class="explain-close" onclick="closeExplain()">✕ Close</button>
    </div>
    <div class="explain-body" id="explain-body"></div>
  </div>
</div>

<div class="loading-overlay" id="overlay">
  <div class="spinner" style="width:36px;height:36px;border-width:4px"></div>
  <div style="color:#94a3b8;font-size:13px" id="overlay-msg">Running cycle…</div>
</div>

<header>
  <div class="logo">NYSE Trading Engine</div>
  <span class="badge" id="mode-badge">—</span>
  <span id="market-status" style="font-size:12px;color:#94a3b8">
    <span class="market-dot market-unknown" id="market-dot"></span>
    <span id="market-label">Market —</span>
  </span>
  <!-- Public ngrok URL badge — visible only when tunnel is active -->
  <div class="public-url-wrap" id="public-url-wrap" style="display:none">
    <span class="public-url-label">🌐 Public</span>
    <span class="public-url-val" id="public-url-val">—</span>
    <button class="btn-copy-url" onclick="copyPublicUrl()" title="Copy public URL">⎘ Copy</button>
  </div>
  <!-- Live unrealized P&L ticker -->
  <div class="pnl-ticker" id="pnl-ticker" style="display:none">
    <div class="pnl-ticker-label">Unrealized P&amp;L</div>
    <div class="pnl-ticker-value" id="pnl-ticker-val">—</div>
    <div class="pnl-ticker-pct" id="pnl-ticker-pct" style="color:#64748b">—</div>
  </div>
  <div class="hdr-right">
    <span class="ts" id="cycle-info" style="color:#475569">—</span>
    <span class="ts" id="last-ts">—</span>
    <span id="notif-indicator" title="Notifications" style="font-size:17px;cursor:default;opacity:.4" onclick="window.location='/stats'">🔔</span>
    <button class="theme-toggle" id="theme-btn" onclick="toggleTheme()" title="Toggle dark/light mode">☀️</button>
    <button class="btn-refresh" onclick="refresh()">Refresh</button>
    <button class="btn-rescan" id="btn-rescan" onclick="rescan()">Re-scan</button>
    <button class="btn-cycle" id="btn-cycle" onclick="runCycle()">Run Cycle</button>
    <button class="btn-refresh" onclick="window.location='/stats'" style="background:#1e3a5f;color:#93c5fd">Stats</button>
    <button class="btn-refresh" onclick="window.location='/journal'" style="background:#1a3020;color:#6ee7b7;border:1px solid #14532d">Journal</button>
    <button class="btn-refresh" onclick="window.location='/settings'" style="background:#1c1a30;color:#c4b5fd;border:1px solid #4c1d95">Settings</button>
    {% if auth %}<a href="/logout" style="padding:7px 14px;border-radius:6px;background:#7f1d1d;color:#fca5a5;font-size:12px;font-weight:600;text-decoration:none;border:1px solid #991b1b;white-space:nowrap">Logout</a>{% endif %}
  </div>
</header>

<main>
  <div class="error-banner" id="err-banner"></div>
  <div id="no-keys-banner" style="{% if alpaca_connected %}display:none{% else %}display:flex{% endif %};background:#1c1508;border:1px solid #92400e;border-radius:8px;padding:12px 16px;margin-bottom:14px;align-items:center;justify-content:space-between;gap:12px">
    <span style="font-size:13px;color:#fbbf24">&#9888; Connect your Alpaca API keys in <a href="/settings" style="color:#fbbf24;text-decoration:underline">Settings</a> to start trading.</span>
  </div>

  <!-- ══ Stock Search & Favorites — always first, impossible to miss ══ -->
  <div class="panel grid1" id="search-panel" style="border:1px solid #0ea5e9;margin-bottom:14px">
    <div class="panel-title" style="justify-content:space-between;flex-wrap:wrap;gap:6px;border-bottom-color:#0ea5e9">
      <span style="color:#38bdf8">🔍 Stock Search &amp; Favorites</span>
      <span style="font-size:11px;color:#475569;font-weight:400">type any ticker · Enter to search · ⭐ pin to save</span>
    </div>
    <div class="search-bar">
      <input type="text" id="search-input" placeholder="e.g. AAPL, TSLA, SPY, QQQ…" maxlength="6"
             oninput="this.value=this.value.toUpperCase()"
             onkeydown="if(event.key==='Enter')searchStock()" autocomplete="off" spellcheck="false"
             style="font-size:15px;padding:10px 14px"/>
      <button class="btn-search" onclick="searchStock()" style="padding:10px 24px;font-size:14px">Search</button>
    </div>
    <div id="search-result" style="display:none"></div>
  </div>

  <!-- Pinned personal watchlist — shown when at least one ticker is pinned -->
  <div class="panel grid1" id="pinned-panel" style="display:none">
    <div class="panel-title">
      ⭐ Pinned Favorites
      <span id="pin-count" style="background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px">0</span>
      <span style="font-size:11px;color:#475569;margin-left:8px">saved between restarts</span>
    </div>
    <div class="pin-grid" id="pin-grid"></div>
  </div>

  <!-- stat cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Total Value</div>
      <div class="card-value neu" id="c-total">—</div>
      <div class="card-sub" id="c-initial">—</div>
    </div>
    <div class="card">
      <div class="card-label">Cash</div>
      <div class="card-value neu" id="c-cash">—</div>
    </div>
    <div class="card">
      <div class="card-label">In Positions</div>
      <div class="card-value neu" id="c-pos-val">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total P&amp;L</div>
      <div class="card-value" id="c-pnl">—</div>
      <div class="card-sub" id="c-pnl-pct">—</div>
    </div>
    <div class="card">
      <div class="card-label">Open Positions</div>
      <div class="card-value neu" id="c-open">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total Trades</div>
      <div class="card-value neu" id="c-trades">—</div>
    </div>
    <div class="card" id="regime-card" style="display:none">
      <div class="card-label">Market Regime</div>
      <div class="card-value" id="c-regime">—</div>
      <div class="card-sub" id="c-regime-sub">—</div>
    </div>
  </div>

  <!-- VOO 200-week MA monitor -->
  <div class="voo-panel" id="voo-panel">
    <div class="voo-header">
      <span class="voo-title">VOO — 200-Week Moving Average Monitor</span>
      <span class="voo-checked" id="voo-checked">—</span>
      <button class="btn-voo" onclick="refreshVOO()">Refresh VOO</button>
    </div>
    <div id="voo-body"><div class="voo-loading">Waiting for first cycle… click Refresh VOO to load now.</div></div>
  </div>

  <!-- sector breakdown pie chart — only shown when positions are open -->
  <div class="panel grid1" id="sector-pie-panel" style="display:none">
    <div class="panel-title">Sector Allocation
      <span class="count" id="sector-pie-count">0</span>
    </div>
    <div class="sector-chart-wrap">
      <div id="sector-pie-plot" style="height:280px"></div>
    </div>
  </div>

  <!-- watchlist scan -->
  <div class="panel grid1" id="scan-panel">
    <div class="panel-title">
      Watchlist
      <span class="count" id="wl-count">0</span>
      <span id="scan-meta" style="font-size:11px;color:#475569;margin-left:8px">—</span>
      <span id="fund-badge" style="display:none;margin-left:auto;font-size:11px;padding:2px 10px;border-radius:99px;background:#14532d;color:#4ade80;font-weight:600"></span>
    </div>
    <div id="fund-bar" style="display:none;padding:8px 16px;border-bottom:1px solid #334155;font-size:12px;color:#94a3b8;display:none;gap:16px;flex-wrap:wrap"></div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;padding:12px 16px" id="wl-chips"></div>
  </div>

  <!-- after-hours / pre-market panel -->
  <div class="panel grid1" id="ext-hours-panel" style="display:none">
    <div class="panel-title">Pre / Post-Market
      <span style="font-size:11px;color:#475569;margin-left:8px" id="ext-hours-note">2-min cache</span>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr>
        <th>Ticker</th><th>Regular Close</th><th>Pre-Market</th><th>Pre Chg</th><th>Post-Market</th><th>Post Chg</th>
      </tr></thead>
      <tbody id="ext-hours-body"><tr><td colspan="6" class="empty">No data</td></tr></tbody>
    </table></div>
  </div>

  <!-- signal analysis -->
  <div class="panel grid1">
    <div class="panel-title">Signal Analysis <span class="count" id="sig-count">0</span>
      <span id="mtf-badge" style="display:none;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:99px;background:#1e3a5f;color:#93c5fd;font-weight:600">MTF ON · 1d 50% · 1h 30% · 15m 20%</span>
      <span id="mr-badge" style="display:none;margin-left:4px;font-size:10px;padding:2px 8px;border-radius:99px;background:#14532d;color:#4ade80;font-weight:600">MR ON</span>
      <span id="corr-badge" style="display:none;margin-left:4px;font-size:10px;padding:2px 8px;border-radius:99px;background:#1e3a5f;color:#93c5fd;font-weight:600">CORR FILTER ON</span>
      <span id="sizing-badge" style="display:none;margin-left:4px;font-size:10px;padding:2px 8px;border-radius:99px;background:#451a03;color:#fdba74;font-weight:600">ADAPTIVE SIZE</span>
      <span id="ml-badge" style="display:none;margin-left:4px;font-size:10px;padding:2px 8px;border-radius:99px;background:#312e81;color:#a5b4fc;font-weight:600">ML RANKING</span>
    </div>
    <div class="tbl-wrap"><table>
      <thead><tr>
        <th>Ticker</th><th>Sector</th><th>Price</th><th>Signal</th><th>Score</th><th>RSI</th><th class="z-col">Z-Score</th><th class="vol-col">Volume</th><th></th>
      </tr></thead>
      <tbody id="sig-body"><tr><td colspan="9" class="empty">No data yet — click Refresh</td></tr></tbody>
    </table></div>
  </div>

  <!-- watchlist heat map -->
  <div class="panel grid1" id="heatmap-panel" style="display:none">
    <div class="panel-title">Watchlist Heat Map
      <span style="font-size:11px;color:#475569;margin-left:6px">daily % change</span>
    </div>
    <div class="hm-grid" id="hm-grid"></div>
  </div>

  <!-- news feed panel -->
  <div class="panel grid1" id="news-panel" style="display:none">
    <div class="panel-title">
      Market News
      <span id="news-count" style="background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px">0</span>
      <span style="font-size:11px;color:#475569;margin-left:8px" id="news-note">15-min cache · watchlist only</span>
      <button id="btn-news-refresh" onclick="loadNews(true)" style="margin-left:auto;background:none;border:1px solid #334155;color:#64748b;font-size:11px;padding:2px 10px;border-radius:99px;min-height:22px;cursor:pointer">↻ Refresh</button>
    </div>
    <div id="news-body"><div class="news-loading">Loading headlines…</div></div>
  </div>

  <!-- positions + trades -->
  <div class="grid2">
    <div class="panel">
      <div class="panel-title">Positions <span class="count" id="pos-count">0</span>
        <span id="trail-badge" style="display:none;margin-left:auto;font-size:10px;padding:2px 8px;border-radius:99px;background:#14532d;color:#4ade80;font-weight:600">TRAILING STOP ON</span>
      </div>
      <div class="sector-strip" id="sector-strip" style="display:none"></div>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th>Ticker</th><th>Sector</th><th>Entry</th><th>Current</th><th id="stop-th">Stop</th><th>Qty</th><th>Unrealized P&amp;L</th>
        </tr></thead>
        <tbody id="pos-body"><tr><td colspan="7" class="empty">No open positions</td></tr></tbody>
      </table></div>
    </div>
    <div class="panel">
      <div class="panel-title">Trades <span class="count" id="trade-count">0</span></div>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>P&amp;L</th>
        </tr></thead>
        <tbody id="trade-body"><tr><td colspan="6" class="empty">No trades yet</td></tr></tbody>
      </table></div>
    </div>
  </div>
</main>

<script>
const fmt = (n, dec=2) => n == null ? '—' : n.toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtD = n => n == null ? '—' : (n>=0?'+':'')+fmt(n);
const cls = n => n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu';

function applyState(s) {
  if (!s || typeof s !== 'object') return;
  window._state = s;
  const p = s.portfolio || {};
  const signals   = s.signals   || [];
  const positions = s.positions || [];
  const trades    = s.trades    || [];

  // no-keys banner
  const noKeysBanner = document.getElementById('no-keys-banner');
  if (noKeysBanner) noKeysBanner.style.display = (s.alpaca_connected === false) ? 'flex' : 'none';

  // mode badge
  const mode = s.mode || 'Local Simulation';
  const badge = document.getElementById('mode-badge');
  badge.textContent = mode;
  badge.className = 'badge ' + (mode.includes('Paper') ? 'badge-paper' : mode.includes('LIVE') ? 'badge-live' : 'badge-sim');

  // market
  const dot = document.getElementById('market-dot');
  const lbl = document.getElementById('market-label');
  if (s.market_open === true) {
    dot.className = 'market-dot market-open'; lbl.textContent = 'Market OPEN';
  } else if (s.market_open === false) {
    dot.className = 'market-dot market-closed'; lbl.textContent = 'Market CLOSED';
  } else {
    dot.className = 'market-dot market-unknown'; lbl.textContent = 'Market —';
  }

  document.getElementById('last-ts').textContent = s.timestamp;
  _nextCycleAt = s.next_cycle_at ? new Date(s.next_cycle_at) : null;
  _lastCycleAt = s.last_cycle_at ? new Date(s.last_cycle_at) : null;
  updateCycleInfo();

  // cards
  document.getElementById('c-total').textContent = '$' + fmt(p.total_value);
  document.getElementById('c-initial').textContent = 'Initial $' + fmt(p.initial_capital);
  document.getElementById('c-cash').textContent = '$' + fmt(p.cash);
  document.getElementById('c-pos-val').textContent = '$' + fmt(p.position_value);

  const pnlEl = document.getElementById('c-pnl');
  pnlEl.textContent = fmtD(p.total_pnl) && ('$' + (p.total_pnl >= 0 ? '+' : '') + fmt(Math.abs(p.total_pnl)));
  pnlEl.className = 'card-value ' + cls(p.total_pnl);
  document.getElementById('c-pnl-pct').textContent = p.total_pnl_pct != null ? ((p.total_pnl_pct >= 0 ? '+' : '') + fmt(p.total_pnl_pct) + '%') : '—';

  document.getElementById('c-open').textContent = p.open_positions;
  document.getElementById('c-trades').textContent = p.total_trades;

  // live P&L ticker — sum unrealized from all open positions
  {
    const unrealized = positions.reduce((sum, pos) => sum + (pos.pnl || 0), 0);
    const basis = (p.total_value || 0) - unrealized;
    const unrealizedPct = basis > 0 ? unrealized / basis * 100 : 0;
    updatePnlTicker(unrealized, unrealizedPct, positions.length);
  }

  // regime card
  const regimeCard = document.getElementById('regime-card');
  const regimeEl   = document.getElementById('c-regime');
  const regimeSubEl= document.getElementById('c-regime-sub');
  if (s.regime) {
    regimeCard.style.display = '';
    const r = s.regime;
    regimeEl.textContent  = r.regime;
    regimeEl.className    = 'card-value regime-' + r.regime.toLowerCase();
    const vixStr = r.vix != null ? `  VIX ${r.vix.toFixed(1)}` : '';
    regimeSubEl.textContent = `SPY $${fmt(r.spy_price)} · SMA200 $${fmt(r.sma200)}${vixStr}`;
  } else {
    regimeCard.style.display = 'none';
  }

  // watchlist chips
  const wl = s.watchlist || [];
  document.getElementById('wl-count').textContent = wl.length;
  const scan = s.scan;
  if (scan) {
    document.getElementById('scan-meta').textContent =
      `scanned ${scan.scanned_at}  ·  volume top ${scan.volume_candidates_count}  ·  signal ranked to ${wl.length}`;

    // fundamental filter badge + bar
    const fundBadge = document.getElementById('fund-badge');
    const fundBar   = document.getElementById('fund-bar');
    if (scan.fund_enabled) {
      fundBadge.style.display = 'inline-block';
      fundBadge.textContent   = `Fundamentals ON  ✓${scan.fund_passed} ✗${scan.fund_failed}`;
      fundBar.style.display   = 'flex';
      fundBar.innerHTML =
        `<span style="color:#4ade80">✓ ${scan.fund_passed} passed</span>` +
        `<span style="color:#f87171">✗ ${scan.fund_failed} filtered out</span>` +
        `<span style="color:#475569">P/E &lt; 30 · D/E &lt; 2 · Positive FCF · Positive EPS growth</span>`;
    } else {
      fundBadge.style.display = 'none';
      fundBar.style.display   = 'none';
    }
  }
  const chips = document.getElementById('wl-chips');
  if (!wl.length) {
    chips.innerHTML = '<span style="color:#475569;font-size:12px">No watchlist yet — waiting for session scan</span>';
  } else {
    chips.innerHTML = wl.map(sym => {
      const action = scan && scan.actions ? scan.actions[sym] : null;
      const score  = scan && scan.scores  ? scan.scores[sym]  : null;
      const col = action === 'BUY' ? '#22c55e' : action === 'SELL' ? '#ef4444' : '#94a3b8';
      const scoreStr = score != null ? ` ${score >= 0 ? '+' : ''}${score}` : '';
      const earnDays = s.earnings_warnings && s.earnings_warnings[sym] != null ? s.earnings_warnings[sym] : null;
      const earnBadge = earnDays != null
        ? `<span style="color:#f97316;font-size:10px;margin-left:4px" title="Earnings in ${earnDays}d — buys blocked">⚠ ${earnDays}d</span>`
        : '';
      return `<span style="background:#1e293b;border:1px solid ${earnDays != null ? '#92400e' : '#334155'};border-radius:6px;padding:5px 10px;font-size:12px;font-weight:600">
        <span style="color:${col}">${sym}</span><span style="color:#475569;font-size:11px">${scoreStr}</span>${earnBadge}
      </span>`;
    }).join('');
  }

  // MTF badge
  const mtfBadge = document.getElementById('mtf-badge');
  if (s.mtf_enabled) mtfBadge.style.display = 'inline-block';
  else               mtfBadge.style.display = 'none';

  // feature badges in signal panel header
  const mrBadge   = document.getElementById('mr-badge');
  const corrBadge = document.getElementById('corr-badge');
  const sizeBadge = document.getElementById('sizing-badge');
  const mlBadge   = document.getElementById('ml-badge');
  if (mrBadge)   mrBadge.style.display   = s.mean_reversion_enabled      ? 'inline-block' : 'none';
  if (corrBadge) corrBadge.style.display = s.correlation_filter_enabled   ? 'inline-block' : 'none';
  if (sizeBadge) sizeBadge.style.display = s.adaptive_sizing_enabled      ? 'inline-block' : 'none';
  if (mlBadge && s.ml_status) {
    if (s.ml_status.trained) {
      mlBadge.style.display = 'inline-block';
      const acc = s.ml_status.accuracy != null ? ` · ${(s.ml_status.accuracy * 100).toFixed(0)}% acc` : '';
      mlBadge.textContent = `ML ON · ${s.ml_status.samples} trades${acc}`;
      mlBadge.title = `Last trained: ${s.ml_status.last_trained || '—'}`;
    } else if (s.ml_status.sklearn_available) {
      mlBadge.style.display = 'inline-block';
      mlBadge.style.opacity = '.5';
      mlBadge.textContent = `ML · ${s.ml_status.samples || 0}/${20} samples`;
      mlBadge.title = 'Needs 20 completed trades to train';
    } else {
      mlBadge.style.display = 'none';
    }
  }

  // signals
  document.getElementById('sig-count').textContent = signals.length;
  const sb = document.getElementById('sig-body');
  if (!signals.length) {
    sb.innerHTML = '<tr><td colspan="9" class="empty">No signals — click Refresh</td></tr>';
  } else {
    sb.innerHTML = signals.map(r => {
      const barPct = Math.round(Math.abs(r.score) * 100);
      const barCol = r.action === 'BUY' ? '#22c55e' : r.action === 'SELL' ? '#ef4444' : '#6b7280';
      const fmtTF  = v => v == null ? '' : `<span style="color:${v>=0?'#4ade80':'#f87171'}">${v>=0?'+':''}${fmt(v,3)}</span>`;
      const agreeStr = r.mtf_agreement != null ? `<span style="color:#64748b"> agree ${r.mtf_agreement}/3</span>` : '';
      const mtfRow = s.mtf_enabled && (r.tf_1d != null || r.tf_1h != null || r.tf_15m != null)
        ? `<div class="mtf-scores">
             <span>1d ${fmtTF(r.tf_1d)}</span>
             <span>1h ${fmtTF(r.tf_1h)}</span>
             <span>15m ${fmtTF(r.tf_15m)}</span>
             ${agreeStr}
           </div>`
        : '';
      const vr = r.volume_ratio;
      const vrCol  = vr == null ? '#475569' : vr >= 3 ? '#f97316' : vr >= 2 ? '#fb923c' : vr >= 1.5 ? '#fbbf24' : '#475569';
      const vrIcon = vr >= 3 ? ' ●' : vr >= 2 ? ' ▲' : '';
      const vrBold = vr >= 1.5 ? 'font-weight:600;' : '';
      const vrStr  = vr == null ? '—' : `${vr.toFixed(1)}×${vrIcon}`;
      const rowHighlight = vr >= 2 ? 'background:rgba(249,115,22,0.05);' : '';

      // Pending confirmation badge
      const isPending = s.confirmation_enabled && (s.pending_confirmation||[]).includes(r.symbol);
      const pendingBadge = isPending
        ? `<span title="Awaiting next-candle confirmation" style="margin-left:5px;font-size:10px;color:#f59e0b;font-weight:700">⏳</span>`
        : '';

      // Correlation-blocked badge
      const corrBlock = r.corr_blocked;
      const corrBadgeCell = corrBlock
        ? `<span title="Blocked: correlated with ${corrBlock}" style="margin-left:5px;font-size:10px;color:#f87171;font-weight:700">ρ</span>`
        : '';

      // Z-score column
      const z = r.z_score;
      const zCol = z == null ? '#475569' : z <= -1.5 ? '#22c55e' : z <= -1.0 ? '#4ade80'
                 : z >= 1.5 ? '#ef4444' : z >= 1.0 ? '#f87171' : '#475569';
      const zBold = z != null && Math.abs(z) >= 1.5 ? 'font-weight:600;' : '';
      const zStr  = z == null ? '—' : (z >= 0 ? '+' : '') + z.toFixed(2);

      // Adaptive size sub-line (shown for BUY signals when adaptive sizing is on)
      const sizeRow = s.adaptive_sizing_enabled && r.est_size_pct != null && r.action === 'BUY'
        ? `<div class="sig-sub"><span style="color:#f59e0b">~${r.est_size_pct}% portfolio</span>` +
          (r.atr_pct != null ? `<span>ATR ${r.atr_pct.toFixed(1)}%</span>` : '') + `</div>`
        : '';

      // ML multiplier sub-line under Score
      const mlRow = s.ml_status && s.ml_status.trained && r.ml_mult != null
        ? `<div class="sig-sub"><span style="color:#a5b4fc">ML×${r.ml_mult.toFixed(2)}</span></div>`
        : '';

      return `<tr style="${rowHighlight}">
        <td class="sym-link" style="font-weight:600" onclick="openChart('${r.symbol}')" title="Click for chart">${r.symbol}${pendingBadge}${corrBadgeCell}</td>
        <td style="color:#64748b;font-size:12px">${r.sector||'—'}</td>
        <td>$${fmt(r.price)}</td>
        <td>
          <span class="pill pill-${r.action}">${r.action}</span>
          ${sizeRow}
        </td>
        <td>
          <div class="score-wrap">
            <span style="color:${barCol};font-weight:600">${r.score >= 0 ? '+' : ''}${fmt(r.score, 3)}</span>
            <div class="score-bar-bg"><div class="score-bar" style="width:${barPct}%;background:${barCol}"></div></div>
          </div>
          ${mtfRow}${mlRow}
        </td>
        <td>${r.rsi != null ? fmt(r.rsi, 1) : '—'}</td>
        <td class="z-col" style="color:${zCol};${zBold}">${zStr}</td>
        <td class="vol-col" style="color:${vrCol};${vrBold}">${vrStr}</td>
        <td><button onclick="explainSignal('${r.symbol}')" style="padding:4px 10px;font-size:11px;font-weight:600;background:rgba(59,130,246,.12);color:#93c5fd;border:1px solid rgba(59,130,246,.25);border-radius:5px;cursor:pointer;white-space:nowrap;min-height:unset" title="Explain this signal in plain English">Explain</button></td>
      </tr>`;
    }).join('');
  }

  // sector exposure strip
  const strip = document.getElementById('sector-strip');
  const maxPerSector = s.max_per_sector || 3;
  const expo = s.sector_exposure || {};
  if (positions.length && Object.keys(expo).length) {
    strip.style.display = 'flex';
    strip.innerHTML = Object.entries(expo).map(([sec, cnt]) => {
      const cls2 = cnt >= maxPerSector ? 'sector-chip at-limit'
                 : cnt >= maxPerSector - 1 ? 'sector-chip near-limit'
                 : 'sector-chip';
      return `<span class="${cls2}">${sec} ${cnt}/${maxPerSector}</span>`;
    }).join('');
  } else {
    strip.style.display = 'none';
  }

  // trailing stop badge
  const trailBadge = document.getElementById('trail-badge');
  if (s.trailing_stop_enabled) {
    trailBadge.style.display = 'inline-block';
    document.getElementById('stop-th').textContent = 'Trail Stop';
  } else {
    trailBadge.style.display = 'none';
    document.getElementById('stop-th').textContent = 'Stop';
  }

  // positions — Ticker, Sector, Entry, Current, Stop/Trail, Qty, Unrealized P&L
  document.getElementById('pos-count').textContent = positions.length;
  const pb = document.getElementById('pos-body');
  if (!positions.length) {
    pb.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
  } else {
    pb.innerHTML = positions.map(p => {
      // Stop column: green when trailing stop has ratcheted above the fixed stop
      const fixedStop = p.entry_price * (1 - 0.05);
      const stopMoved = s.trailing_stop_enabled && p.stop_loss > fixedStop * 1.001;
      const stopCol = stopMoved ? '#4ade80' : '#94a3b8';
      const stopTip = stopMoved
        ? `title="High: $${fmt(p.highest_price)}  Locked in ${fmt((p.stop_loss/p.entry_price-1)*100,1)}%"`
        : '';
      return `<tr>
        <td style="font-weight:600">${p.symbol}</td>
        <td style="color:#64748b;font-size:12px">${p.sector||'—'}</td>
        <td>$${fmt(p.entry_price)}</td>
        <td>$${fmt(p.current_price)}</td>
        <td style="color:${stopCol};font-size:12px" ${stopTip}>$${fmt(p.stop_loss)}${stopMoved?' ↑':''}</td>
        <td>${p.shares}</td>
        <td class="${cls(p.pnl)}">${p.pnl >= 0 ? '+' : ''}$${fmt(Math.abs(p.pnl))} (${p.pnl_pct >= 0 ? '+' : ''}${fmt(p.pnl_pct)}%)</td>
      </tr>`;
    }).join('');
  }

  // trades — columns: Time, Ticker, Side, Qty, Price, Realized P&L
  document.getElementById('trade-count').textContent = trades.length;
  const tb = document.getElementById('trade-body');
  if (!trades.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">No trades yet</td></tr>';
  } else {
    tb.innerHTML = trades.map(t => `<tr>
      <td style="color:#64748b;font-size:12px">${t.timestamp}</td>
      <td style="font-weight:600">${t.symbol}</td>
      <td><span class="pill pill-${t.action}">${t.action}</span></td>
      <td>${t.shares}</td>
      <td>$${fmt(t.price)}</td>
      <td class="${t.pnl != null ? cls(t.pnl) : 'neu'}">${t.pnl != null ? (t.pnl >= 0 ? '+' : '') + '$' + fmt(Math.abs(t.pnl)) + ' (' + (t.pnl_pct >= 0 ? '+' : '') + fmt(t.pnl_pct) + '%)' : '—'}</td>
    </tr>`).join('');
  }

  // sector allocation pie
  renderSectorPie(positions);

  // after-hours panel
  renderExtHours(s.extended_hours || [], s.market_open);

  // notification bell — full opacity when at least one channel is configured
  const bell = document.getElementById('notif-indicator');
  if (s.notifications && (s.notifications.ntfy || s.notifications.pushover)) {
    bell.style.opacity = '1';
    bell.title = 'Notifications ON — click for Stats';
  } else {
    bell.style.opacity = '.35';
    bell.title = 'Notifications OFF — click for setup';
  }

  // VOO panel
  renderVOO(s.voo);

  // public URL
  if (s.public_url) updatePublicUrl(s.public_url);

  // error
  const eb = document.getElementById('err-banner');
  if (s.error) { eb.textContent = '⚠ ' + s.error; eb.style.display = 'block'; }
  else { eb.style.display = 'none'; }
}

// ── Sector allocation pie chart ───────────────────────────────────────────────
function renderSectorPie(positions) {
  const panel = document.getElementById('sector-pie-panel');
  const el    = document.getElementById('sector-pie-plot');
  const ct    = document.getElementById('sector-pie-count');
  if (!positions || !positions.length) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  ct.textContent = positions.length;

  const byValue = {};
  positions.forEach(p => {
    const sec = (p.sector && p.sector !== '—') ? p.sector : 'Other';
    byValue[sec] = (byValue[sec] || 0) + (p.current_price * p.shares);
  });
  const labels = Object.keys(byValue);
  const values = labels.map(l => Math.round(byValue[l] * 100) / 100);

  const PALETTE = ['#3b82f6','#22c55e','#f59e0b','#ef4444','#8b5cf6',
                   '#ec4899','#14b8a6','#f97316','#84cc16','#06b6d4','#a855f7'];
  const isLight = document.body.classList.contains('light');
  const bg      = isLight ? 'rgba(255,255,255,0)' : 'rgba(0,0,0,0)';
  const lineCol = isLight ? '#ffffff' : '#1e293b';
  const fontCol = isLight ? '#475569' : '#94a3b8';

  Plotly.react(el, [{
    type: 'pie', labels, values, hole: 0.38,
    textinfo: 'label+percent',
    textfont: {size: 11, color: fontCol},
    marker: {colors: PALETTE, line: {color: lineCol, width: 2}},
    hovertemplate: '<b>%{label}</b><br>$%{value:,.0f}<br>%{percent}<extra></extra>',
  }], {
    paper_bgcolor: bg, plot_bgcolor: bg,
    font: {color: fontCol, family: 'Segoe UI,system-ui,sans-serif', size: 11},
    margin: {l: 10, r: 10, t: 10, b: 10},
    legend: {font: {color: fontCol, size: 11}, bgcolor: 'rgba(0,0,0,0)', orientation: 'v'},
    showlegend: true,
  }, {responsive: true, displayModeBar: false});
}

// ── Watchlist heat map ────────────────────────────────────────────────────────
function renderHeatmap(items) {
  const panel = document.getElementById('heatmap-panel');
  const grid  = document.getElementById('hm-grid');
  if (!items || !items.length) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const isLight = document.body.classList.contains('light');

  grid.innerHTML = items.map(item => {
    const pct  = item.change_pct;
    const abs  = Math.abs(pct);
    const intensity = Math.min(1, abs / 2.5);   // saturates at ±2.5%
    const alpha = 0.12 + intensity * 0.55;
    const isUp  = pct >= 0;
    const bgCol = isUp
      ? (isLight ? `rgba(21,128,61,${alpha})` : `rgba(34,197,94,${alpha})`)
      : (isLight ? `rgba(185,28,28,${alpha})` : `rgba(239,68,68,${alpha})`);
    const pctCol = isUp
      ? (intensity > 0.35 ? '#4ade80' : '#22c55e')
      : (intensity > 0.35 ? '#f87171' : '#ef4444');
    const sign  = pct >= 0 ? '+' : '';
    return `<div class="hm-cell" style="background:${bgCol}">
      <div class="hm-sym">${item.symbol}</div>
      <div class="hm-pct" style="color:${pctCol}">${sign}${pct.toFixed(2)}%</div>
      <div class="hm-price">$${item.price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}</div>
    </div>`;
  }).join('');
}

async function loadHeatmap() {
  try {
    const res  = await fetch('/api/heatmap');
    const data = await res.json();
    if (data.ok) renderHeatmap(data.items);
  } catch(_) {}
}

function renderExtHours(rows, marketOpen) {
  const panel = document.getElementById('ext-hours-panel');
  const body  = document.getElementById('ext-hours-body');
  const note  = document.getElementById('ext-hours-note');

  if (!rows || !rows.length) { panel.style.display = 'none'; return; }
  panel.style.display = '';

  const hasAnyExt = rows.some(r => r.pre_market_price != null || r.post_market_price != null);
  if (!hasAnyExt) {
    note.textContent = marketOpen ? 'Market is open — extended hours data not available' : '2-min cache';
    body.innerHTML = '<tr><td colspan="6" class="empty" style="font-size:12px">' +
      (marketOpen ? 'Pre/post-market prices are unavailable while market is open.' : 'No extended-hours data available.') +
      '</td></tr>';
    return;
  }
  note.textContent = '2-min cache';

  const chgCell = (price, pct) => {
    if (price == null) return '<td style="color:#475569">—</td><td style="color:#475569">—</td>';
    const col = pct == null ? '#e2e8f0' : pct > 0 ? '#22c55e' : pct < 0 ? '#ef4444' : '#94a3b8';
    const pctStr = pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    return `<td>$${fmt(price)}</td><td style="color:${col};font-weight:600">${pctStr}</td>`;
  };
  body.innerHTML = rows.map(r =>
    `<tr>
      <td style="font-weight:600">${r.symbol}</td>
      <td>${r.regular_price != null ? '$' + fmt(r.regular_price) : '—'}</td>
      ${chgCell(r.pre_market_price, r.pre_market_change_pct)}
      ${chgCell(r.post_market_price, r.post_market_change_pct)}
    </tr>`
  ).join('');
}

function renderVOO(voo) {
  const body = document.getElementById('voo-body');
  const checked = document.getElementById('voo-checked');
  if (!voo) {
    body.innerHTML = '<div class="voo-loading">Waiting for first cycle… click Refresh VOO to load now.</div>';
    checked.textContent = '—';
    return;
  }
  checked.textContent = 'Updated ' + voo.checked_at;

  const gapSign = voo.gap_pct >= 0 ? '+' : '';
  const gapCol  = voo.above_ma ? '#4ade80' : '#f87171';
  const alertBar = voo.above_ma
    ? `<div class="voo-alert-bar voo-above">
         <span style="font-size:16px">✓</span>
         VOO is <strong>ABOVE</strong> the 200-Week MA — broad market in long-term uptrend
       </div>`
    : `<div class="voo-alert-bar voo-below">
         <span style="font-size:18px">🟢</span>
         BUY ALERT — VOO is <strong>BELOW</strong> the 200-Week MA — rare long-term buying opportunity
       </div>`;

  body.innerHTML = `
    <div class="voo-stats">
      <div class="voo-stat">
        <div class="voo-stat-label">VOO Price</div>
        <div class="voo-stat-value neu">$${fmt(voo.price)}</div>
      </div>
      <div class="voo-stat">
        <div class="voo-stat-label">200-Week MA</div>
        <div class="voo-stat-value neu">$${fmt(voo.ma200w)}</div>
      </div>
      <div class="voo-stat">
        <div class="voo-stat-label">Gap vs MA</div>
        <div class="voo-stat-value" style="color:${gapCol}">${gapSign}${fmt(voo.gap_pct, 1)}%</div>
      </div>
    </div>
    ${alertBar}`;
}

async function refreshVOO() {
  const btn = document.querySelector('.btn-voo');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  try {
    const res  = await fetch('/api/voo', {method: 'POST'});
    const data = await res.json();
    if (data.ok) renderVOO(data.voo);
    else {
      document.getElementById('err-banner').textContent = 'VOO fetch error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('err-banner').textContent = 'VOO fetch failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Refresh VOO';
  }
}

async function refresh() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    applyState(data);
    loadHeatmap();
  } catch(e) {
    document.getElementById('err-banner').textContent = 'Failed to fetch state: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  }
}

async function rescan() {
  const btn = document.getElementById('btn-rescan');
  const overlay = document.getElementById('overlay');
  document.getElementById('overlay-msg').textContent = 'Scanning S&P 500 universe…';
  btn.disabled = true;
  overlay.classList.add('active');
  const timer = setTimeout(() => {
    overlay.classList.remove('active');
    document.getElementById('overlay-msg').textContent = 'Running cycle…';
    btn.disabled = false;
    document.getElementById('err-banner').textContent = 'Scan timed out — yfinance may be slow. Try again shortly.';
    document.getElementById('err-banner').style.display = 'block';
  }, 120000);
  try {
    const res = await fetch('/api/rescan', {method:'POST'});
    clearTimeout(timer);
    const data = await res.json();
    if (data.ok) applyState(data.state);
    else {
      document.getElementById('err-banner').textContent = 'Scan error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    clearTimeout(timer);
    document.getElementById('err-banner').textContent = 'Scan failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    overlay.classList.remove('active');
    document.getElementById('overlay-msg').textContent = 'Running cycle…';
  }
}

async function runCycle() {
  const btn = document.getElementById('btn-cycle');
  const overlay = document.getElementById('overlay');
  btn.disabled = true;
  document.getElementById('overlay-msg').textContent = 'Running cycle — scanning universe & fetching quotes… (60-90 s)';
  overlay.classList.add('active');
  const timer = setTimeout(() => {
    overlay.classList.remove('active');
    btn.disabled = false;
    document.getElementById('err-banner').textContent = 'Cycle timed out — yfinance may be slow (market closed?). Click Refresh to check status.';
    document.getElementById('err-banner').style.display = 'block';
  }, 120000);
  try {
    const res = await fetch('/api/cycle', {method:'POST'});
    clearTimeout(timer);
    const data = await res.json();
    if (data.ok) applyState(data.state);
    else {
      document.getElementById('err-banner').textContent = 'Cycle error: ' + data.error;
      document.getElementById('err-banner').style.display = 'block';
    }
  } catch(e) {
    clearTimeout(timer);
    document.getElementById('err-banner').textContent = 'Cycle failed: ' + e;
    document.getElementById('err-banner').style.display = 'block';
  } finally {
    btn.disabled = false;
    overlay.classList.remove('active');
  }
}

// ── Dark / light theme ────────────────────────────────────────────────────────
function toggleTheme() {
  const light = document.body.classList.toggle('light');
  localStorage.setItem('theme', light ? 'light' : 'dark');
  document.getElementById('theme-btn').textContent = light ? '🌙' : '☀️';
}
(function initTheme() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = '🌙';
  }
})();

// ── Live P&L ticker ───────────────────────────────────────────────────────────
let _prevPnlVal = null;
function updatePnlTicker(unrealized, unrealizedPct, openPositions) {
  const ticker = document.getElementById('pnl-ticker');
  const valEl  = document.getElementById('pnl-ticker-val');
  const pctEl  = document.getElementById('pnl-ticker-pct');
  if (!openPositions || unrealized == null) { ticker.style.display = 'none'; return; }
  ticker.style.display = '';
  const sign = unrealized >= 0 ? '+' : '';
  const col  = unrealized > 0 ? '#22c55e' : unrealized < 0 ? '#ef4444' : '#94a3b8';
  if (_prevPnlVal !== null && _prevPnlVal !== unrealized) {
    valEl.classList.remove('pnl-flash');
    void valEl.offsetWidth;   // force reflow to restart animation
    valEl.classList.add('pnl-flash');
  }
  _prevPnlVal = unrealized;
  valEl.textContent = sign + '$' + Math.abs(unrealized).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  valEl.style.color = col;
  pctEl.textContent = sign + unrealizedPct.toFixed(2) + '%';
  pctEl.style.color = col;
}

async function pollPnl() {
  try {
    const res  = await fetch('/api/pnl');
    const data = await res.json();
    if (data.ok) updatePnlTicker(data.unrealized_pnl, data.unrealized_pnl_pct, data.open_positions);
  } catch(_) {}
}
// Poll the lightweight pnl endpoint between full state refreshes
setInterval(pollPnl, 5000);
pollPnl();

// Countdown ticker — updates every second without a server round-trip
let _nextCycleAt = null;
let _lastCycleAt = null;
function updateCycleInfo() {
  const el = document.getElementById('cycle-info');
  if (!_nextCycleAt) { el.textContent = ''; return; }
  const secsLeft = Math.max(0, Math.round((_nextCycleAt - Date.now()) / 1000));
  let parts = [];
  if (_lastCycleAt) {
    const ago = Math.round((Date.now() - _lastCycleAt) / 1000);
    parts.push('Last cycle ' + (ago < 60 ? ago + 's ago' : Math.round(ago/60) + 'm ago'));
  }
  parts.push('Next in ' + secsLeft + 's');
  el.textContent = parts.join('  ·  ');
}
setInterval(updateCycleInfo, 1000);

// Initial load + auto-refresh every 5s (picks up background cycle results quickly)
refresh();
setInterval(refresh, 5000);
// Heat map refreshes on each full state refresh (called inside refresh()) — also once on init
loadHeatmap();

// ── Chart modal ───────────────────────────────────────────────────────────────
async function openChart(symbol) {
  const modal = document.getElementById('chart-modal');
  modal.classList.add('active');
  document.getElementById('chart-sym').textContent = symbol;
  document.getElementById('chart-meta').textContent = 'Loading…';
  document.getElementById('chart-plotly').innerHTML = '';
  try {
    const res  = await fetch('/api/chart/' + symbol);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    document.getElementById('chart-meta').textContent = '90-day daily  ·  EMA ' + data.ema_fast_period + '/' + data.ema_slow_period + '  ·  BB  ·  RSI';
    renderChart(data);
  } catch(e) {
    document.getElementById('chart-plotly').innerHTML =
      '<div style="color:#f87171;padding:32px;text-align:center">Chart error: ' + e + '</div>';
    document.getElementById('chart-meta').textContent = '';
  }
}

function closeChart() {
  document.getElementById('chart-modal').classList.remove('active');
  if (window.Plotly) Plotly.purge('chart-plotly');
}

// ── Explain Trade modal ───────────────────────────────────────────────────────
function explainSignal(sym) {
  const s = window._state;
  if (!s) return;
  const r = (s.signals || []).find(x => x.symbol === sym);
  if (!r) return;

  // helpers
  const fmtN = (v, d=1) => v == null ? null : Number(v).toFixed(d);
  const pct   = v => v == null ? null : (v * 100).toFixed(1) + '%';

  const items = [];

  // ── RSI ──────────────────────────────────────────────────────────────────
  if (r.rsi != null) {
    const v = r.rsi;
    let tone, label, detail;
    if (v < 30) {
      tone = 'bull'; label = 'Oversold (Bullish)';
      detail = `RSI is <span class="explain-val">${fmtN(v)}</span> — below 30 signals oversold territory. The stock may be undervalued and due for a bounce.`;
    } else if (v < 45) {
      tone = 'bull'; label = 'Mildly Oversold';
      detail = `RSI is <span class="explain-val">${fmtN(v)}</span> — below 45, leaning oversold. Selling pressure is easing.`;
    } else if (v < 55) {
      tone = 'neu'; label = 'Neutral';
      detail = `RSI is <span class="explain-val">${fmtN(v)}</span> — in the neutral zone, no strong directional bias.`;
    } else if (v < 70) {
      tone = 'bear'; label = 'Mildly Overbought';
      detail = `RSI is <span class="explain-val">${fmtN(v)}</span> — above 55, leaning overbought. Buying momentum is strong but watch for a pullback.`;
    } else {
      tone = 'bear'; label = 'Overbought (Bearish)';
      detail = `RSI is <span class="explain-val">${fmtN(v)}</span> — above 70 signals overbought territory. The stock may be due for a correction.`;
    }
    items.push({icon: tone === 'bull' ? '📊' : tone === 'bear' ? '📊' : '📊', tone, label, detail});
  }

  // ── MACD ──────────────────────────────────────────────────────────────────
  if (r.macd_hist != null) {
    const h = r.macd_hist, hp = r.macd_hist_prev;
    let tone, label, detail;
    const crossed = hp != null && ((h > 0 && hp <= 0) || (h < 0 && hp >= 0));
    if (h > 0 && crossed) {
      tone = 'bull'; label = 'MACD Bullish Crossover';
      detail = `MACD histogram just crossed above zero — a fresh bullish signal indicating momentum is turning upward.`;
    } else if (h > 0 && hp != null && h > hp) {
      tone = 'bull'; label = 'MACD Bullish & Strengthening';
      detail = `MACD histogram is <span class="explain-val">positive and rising</span> — bullish momentum is building.`;
    } else if (h > 0) {
      tone = 'bull'; label = 'MACD Bullish (Fading)';
      detail = `MACD histogram is positive but <span class="explain-val">declining</span> — bullish momentum exists but may be weakening.`;
    } else if (h < 0 && crossed) {
      tone = 'bear'; label = 'MACD Bearish Crossover';
      detail = `MACD histogram just crossed below zero — a fresh bearish signal indicating momentum is turning downward.`;
    } else if (h < 0 && hp != null && h < hp) {
      tone = 'bear'; label = 'MACD Bearish & Strengthening';
      detail = `MACD histogram is <span class="explain-val">negative and falling</span> — bearish momentum is building.`;
    } else {
      tone = 'bear'; label = 'MACD Bearish (Recovering)';
      detail = `MACD histogram is negative but <span class="explain-val">recovering</span> — bearish momentum may be easing.`;
    }
    items.push({icon: '📈', tone, label, detail});
  }

  // ── EMA Trend ──────────────────────────────────────────────────────────────
  if (r.ema_fast != null && r.ema_slow != null) {
    const ef = r.ema_fast, es = r.ema_slow;
    const spread = ((ef - es) / es * 100).toFixed(2);
    const tone = ef > es ? 'bull' : 'bear';
    const label = ef > es ? 'EMA Uptrend' : 'EMA Downtrend';
    const detail = ef > es
      ? `Fast EMA (<span class="explain-val">$${fmtN(ef,2)}</span>) is above slow EMA (<span class="explain-val">$${fmtN(es,2)}</span>) — the stock is in a short-term <strong>uptrend</strong> (spread ${spread}%).`
      : `Fast EMA (<span class="explain-val">$${fmtN(ef,2)}</span>) is below slow EMA (<span class="explain-val">$${fmtN(es,2)}</span>) — the stock is in a short-term <strong>downtrend</strong> (spread ${spread}%).`;
    items.push({icon: '📉', tone, label, detail});
  }

  // ── Bollinger Bands ────────────────────────────────────────────────────────
  if (r.bb_upper != null && r.bb_lower != null && r.price) {
    const p = r.price, bu = r.bb_upper, bl = r.bb_lower;
    let tone, label, detail;
    if (p < bl) {
      tone = 'bull'; label = 'Below Lower Bollinger Band';
      detail = `Price <span class="explain-val">$${fmtN(p,2)}</span> is below the lower band (<span class="explain-val">$${fmtN(bl,2)}</span>) — statistically oversold. Mean-reversion setups often appear here.`;
    } else if (p > bu) {
      tone = 'bear'; label = 'Above Upper Bollinger Band';
      detail = `Price <span class="explain-val">$${fmtN(p,2)}</span> is above the upper band (<span class="explain-val">$${fmtN(bu,2)}</span>) — statistically overbought. The stock is extended above its normal range.`;
    } else {
      const pos = Math.round((p - bl) / (bu - bl) * 100);
      tone = 'neu'; label = 'Within Bollinger Bands';
      detail = `Price is within the bands at <span class="explain-val">${pos}%</span> of the range (lower <span class="explain-val">$${fmtN(bl,2)}</span> → upper <span class="explain-val">$${fmtN(bu,2)}</span>). Normal trading range.`;
    }
    items.push({icon: '〰️', tone, label, detail});
  }

  // ── Z-score ────────────────────────────────────────────────────────────────
  if (r.z_score != null) {
    const z = r.z_score;
    let tone, label, detail;
    if (z <= -2) {
      tone = 'bull'; label = 'Deeply Oversold (Z-Score)';
      detail = `Z-score is <span class="explain-val">${fmtN(z,2)}</span> — price is more than 2 standard deviations below its 20-day mean. Strong mean-reversion candidate.`;
    } else if (z <= -1) {
      tone = 'bull'; label = 'Below Average (Z-Score)';
      detail = `Z-score is <span class="explain-val">${fmtN(z,2)}</span> — price is below its recent average, a mild mean-reversion opportunity.`;
    } else if (z < 1) {
      tone = 'neu'; label = 'Near Average (Z-Score)';
      detail = `Z-score is <span class="explain-val">${fmtN(z,2)}</span> — price is close to its 20-day average. No strong mean-reversion signal.`;
    } else if (z < 2) {
      tone = 'bear'; label = 'Above Average (Z-Score)';
      detail = `Z-score is <span class="explain-val">${fmtN(z,2)}</span> — price is above its recent average. Mild overextension.`;
    } else {
      tone = 'bear'; label = 'Significantly Extended (Z-Score)';
      detail = `Z-score is <span class="explain-val">${fmtN(z,2)}</span> — price is more than 2 standard deviations above its 20-day mean. May be overextended.`;
    }
    items.push({icon: '📐', tone, label, detail});
  }

  // ── Momentum (ROC) ─────────────────────────────────────────────────────────
  if (r.roc_10 != null) {
    const roc = r.roc_10;
    let tone, label, detail;
    if (roc > 0.08) {
      tone = 'bull'; label = 'Strong Upward Momentum';
      detail = `Price is up <span class="explain-val">${pct(roc)}</span> over the last 10 days — strong bullish momentum.`;
    } else if (roc > 0.02) {
      tone = 'bull'; label = 'Mild Upward Momentum';
      detail = `Price is up <span class="explain-val">${pct(roc)}</span> over the last 10 days — positive but modest momentum.`;
    } else if (roc > -0.02) {
      tone = 'neu'; label = 'Flat Momentum';
      detail = `Price has moved <span class="explain-val">${pct(roc)}</span> over the last 10 days — essentially flat, no directional momentum.`;
    } else if (roc > -0.08) {
      tone = 'bear'; label = 'Mild Downward Momentum';
      detail = `Price is down <span class="explain-val">${pct(roc)}</span> over the last 10 days — moderate selling pressure.`;
    } else {
      tone = 'bear'; label = 'Strong Downward Momentum';
      detail = `Price is down <span class="explain-val">${pct(roc)}</span> over the last 10 days — heavy selling pressure.`;
    }
    items.push({icon: '🚀', tone, label, detail});
  }

  // ── StochRSI ───────────────────────────────────────────────────────────────
  if (r.stoch_rsi != null) {
    const sr = r.stoch_rsi;
    let tone, label, detail;
    if (sr < 20) {
      tone = 'bull'; label = 'StochRSI Oversold';
      detail = `StochRSI is <span class="explain-val">${fmtN(sr)}</span> — deeply oversold momentum reading. Historically a precursor to short-term bounces.`;
    } else if (sr > 80) {
      tone = 'bear'; label = 'StochRSI Overbought';
      detail = `StochRSI is <span class="explain-val">${fmtN(sr)}</span> — deeply overbought momentum reading. Pullbacks are more common at these levels.`;
    } else {
      tone = 'neu'; label = 'StochRSI Neutral';
      detail = `StochRSI is <span class="explain-val">${fmtN(sr)}</span> — in the neutral 20–80 range, no extreme momentum signal.`;
    }
    items.push({icon: '⚡', tone, label, detail});
  }

  // ── Volume ─────────────────────────────────────────────────────────────────
  if (r.volume_ratio != null) {
    const vr = r.volume_ratio;
    let tone, label, detail;
    if (vr >= 3) {
      tone = 'bull'; label = 'Exceptional Volume';
      detail = `Trading at <span class="explain-val">${vr.toFixed(1)}×</span> its average volume — unusually high activity often signals institutional interest or a major catalyst.`;
    } else if (vr >= 2) {
      tone = 'bull'; label = 'High Volume';
      detail = `Trading at <span class="explain-val">${vr.toFixed(1)}×</span> its average volume — elevated participation lends conviction to the current move.`;
    } else if (vr >= 1.2) {
      tone = 'neu'; label = 'Above-Average Volume';
      detail = `Trading at <span class="explain-val">${vr.toFixed(1)}×</span> its average volume — slightly elevated, adds modest confirmation.`;
    } else {
      tone = 'neu'; label = 'Normal Volume';
      detail = `Trading at <span class="explain-val">${vr.toFixed(1)}×</span> average — normal volume. The signal lacks volume confirmation.`;
    }
    items.push({icon: '📦', tone, label, detail});
  }

  // ── Build HTML ─────────────────────────────────────────────────────────────
  const scoreOut10 = (Math.abs(r.score) * 10).toFixed(1);
  const scoreHtml = `<div class="explain-score">
    Overall composite score: <strong>${r.score >= 0 ? '+' : ''}${r.score} / ±1.0</strong>
    &nbsp;·&nbsp; Confidence: <strong>${Math.round(r.confidence * 100)}%</strong>
    ${r.ml_mult != null ? `&nbsp;·&nbsp; ML rank multiplier: <strong>${r.ml_mult.toFixed(2)}×</strong>` : ''}
  </div>`;

  const itemsHtml = items.map(it => `
    <div class="explain-item">
      <div class="explain-icon ei-${it.tone}">${it.icon}</div>
      <div class="explain-text">
        <div class="explain-label el-${it.tone}">${it.label}</div>
        <div class="explain-detail">${it.detail}</div>
      </div>
    </div>`).join('');

  const reasonsHtml = r.reasons && r.reasons.length
    ? `<div class="explain-reasons">
        <div class="explain-reasons-title">Algorithm signal reasons</div>
        ${r.reasons.map(re => `<div class="explain-reason">· ${re}</div>`).join('')}
       </div>`
    : '';

  // Update modal
  document.getElementById('explain-sym').textContent = sym;
  const pill = document.getElementById('explain-pill');
  pill.className = `pill pill-${r.action}`;
  pill.textContent = r.action + ' SIGNAL';
  document.getElementById('explain-body').innerHTML = scoreHtml + itemsHtml + reasonsHtml;
  document.getElementById('explain-modal').classList.add('active');
}

function closeExplain() {
  document.getElementById('explain-modal').classList.remove('active');
}

// ── Stock search & personal watchlist ────────────────────────────────────────
async function searchStock() {
  const input = document.getElementById('search-input');
  const sym   = input.value.trim().toUpperCase();
  if (!sym) return;
  const resultEl = document.getElementById('search-result');
  resultEl.style.display = '';
  resultEl.innerHTML = '<div style="padding:14px;color:#64748b;font-size:13px">Searching <b>' + sym + '</b>…</div>';
  try {
    const res  = await fetch('/api/search/' + sym);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    showSearchResult(data);
  } catch(e) {
    resultEl.innerHTML = '<div style="padding:14px;color:#f87171;font-size:13px">Error: ' + e.message + '</div>';
  }
}

function _fmtCap(n) {
  if (!n) return '—';
  if (n >= 1e12) return '$' + (n/1e12).toFixed(1) + 'T';
  if (n >= 1e9)  return '$' + (n/1e9).toFixed(1)  + 'B';
  return '$' + (n/1e6).toFixed(0) + 'M';
}

function showSearchResult(d) {
  const el = document.getElementById('search-result');
  const scoreStr  = d.score != null ? (d.score >= 0 ? '+' : '') + d.score.toFixed(3) : '—';
  const scoreCol  = d.score > 0 ? '#4ade80' : d.score < 0 ? '#f87171' : '#94a3b8';
  const rocStr    = d.roc_10 != null ? (d.roc_10 >= 0 ? '+' : '') + d.roc_10.toFixed(2) + '%' : '—';
  const rocCol    = d.roc_10 == null ? '#94a3b8' : d.roc_10 >= 2 ? '#4ade80' : d.roc_10 <= -2 ? '#f87171' : '#e2e8f0';
  const srsiStr   = d.stoch_rsi != null ? d.stoch_rsi.toFixed(0) : '—';
  const srsiCol   = d.stoch_rsi == null ? '#94a3b8' : d.stoch_rsi < 20 ? '#4ade80' : d.stoch_rsi > 80 ? '#f87171' : '#e2e8f0';
  const pinBtn    = d.pinned
    ? `<button class="btn-pin btn-pin-rem" onclick="unpinStock('${d.symbol}')">✕ Unpin</button>`
    : `<button class="btn-pin btn-pin-add" onclick="pinStock('${d.symbol}')">⭐ Pin</button>`;
  el.innerHTML = `<div class="search-result">
    <div class="sr-header">
      <span class="sr-name">${d.symbol}</span>
      <span class="sr-company">${d.name || ''}</span>
      <span class="pill pill-${d.action||'HOLD'}">${d.action||'HOLD'}</span>
      ${pinBtn}
    </div>
    <div class="sr-stats">
      <div class="sr-stat"><div class="sr-stat-label">Price</div><div class="sr-stat-value">${d.price != null ? '$'+fmt(d.price) : '—'}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">RSI</div><div class="sr-stat-value">${d.rsi != null ? d.rsi : '—'}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">Momentum (10d)</div><div class="sr-stat-value" style="color:${rocCol}">${rocStr}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">StochRSI</div><div class="sr-stat-value" style="color:${srsiCol}">${srsiStr}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">Score</div><div class="sr-stat-value" style="color:${scoreCol}">${scoreStr}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">Sector</div><div class="sr-stat-value" style="font-size:13px">${d.sector||'—'}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">P/E</div><div class="sr-stat-value">${d.pe_ratio != null ? d.pe_ratio : '—'}</div></div>
      <div class="sr-stat"><div class="sr-stat-label">Mkt Cap</div><div class="sr-stat-value" style="font-size:13px">${_fmtCap(d.market_cap)}</div></div>
    </div>
  </div>`;
}

async function pinStock(sym) {
  try {
    await fetch('/api/watchlist/add', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({symbol: sym}),
    });
    // Refresh search result to flip pin button state
    const res = await fetch('/api/search/' + sym);
    const d   = await res.json();
    if (d.ok) showSearchResult(d);
    loadPinnedWatchlist();
  } catch(e) {}
}

async function unpinStock(sym) {
  try {
    await fetch('/api/watchlist/remove', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({symbol: sym}),
    });
    const res = await fetch('/api/search/' + sym);
    const d   = await res.json();
    if (d.ok) showSearchResult(d);
    loadPinnedWatchlist();
  } catch(e) {}
}

async function loadPinnedWatchlist() {
  try {
    const res   = await fetch('/api/watchlist');
    const data  = await res.json();
    const panel = document.getElementById('pinned-panel');
    const grid  = document.getElementById('pin-grid');
    const count = document.getElementById('pin-count');
    if (!data.ok || !data.items || !data.items.length) {
      panel.style.display = 'none'; return;
    }
    panel.style.display = '';
    count.textContent   = data.items.length;
    grid.innerHTML = data.items.map(item => {
      const chgCol = item.change_pct == null ? '#94a3b8'
                   : item.change_pct >= 0 ? '#22c55e' : '#ef4444';
      const chgStr = item.change_pct != null
                   ? (item.change_pct >= 0 ? '+' : '') + item.change_pct.toFixed(2) + '%'
                   : '—';
      const act = item.action && item.action !== '—' ? item.action : 'HOLD';
      const scoreStr = item.score != null ? (item.score >= 0 ? '+' : '') + item.score.toFixed(3) : null;
      const scoreCol = item.score == null ? '#94a3b8' : item.score > 0 ? '#4ade80' : '#f87171';
      return `<div class="pin-card">
        <button class="pin-remove" onclick="unpinStock('${item.symbol}')" title="Unpin">✕</button>
        <div class="pin-sym">${item.symbol}</div>
        <div class="pin-price">${item.price != null ? '$'+fmt(item.price) : '—'}</div>
        <div class="pin-change" style="color:${chgCol}">${chgStr}</div>
        <div style="margin-top:5px"><span class="pill pill-${act}" style="font-size:10px">${act}</span></div>
        ${item.rsi != null ? '<div class="pin-rsi">RSI '+item.rsi+'</div>' : ''}
        ${scoreStr ? '<div class="pin-rsi" style="color:'+scoreCol+'">Score '+scoreStr+'</div>' : ''}
      </div>`;
    }).join('');
  } catch(e) {}
}

// ── Public URL ─────────────────────────────────────────────────────────────────
function updatePublicUrl(url) {
  const wrap = document.getElementById('public-url-wrap');
  const val  = document.getElementById('public-url-val');
  if (!url || !wrap) return;
  wrap.style.display = 'flex';
  val.textContent    = url;
}

function copyPublicUrl() {
  const val = document.getElementById('public-url-val');
  if (!val) return;
  const url = val.textContent;
  (navigator.clipboard
    ? navigator.clipboard.writeText(url)
    : Promise.reject()
  ).then(() => {
    const orig = val.textContent;
    val.textContent = 'Copied!';
    setTimeout(() => { val.textContent = orig; }, 1500);
  }).catch(() => { window.prompt('Copy this URL:', url); });
}

// Load pinned watchlist on init; refresh every 30s
loadPinnedWatchlist();
setInterval(loadPinnedWatchlist, 30000);

// ── News feed ─────────────────────────────────────────────────────────────────
let _newsLoaded = false;
function _relTime(ts) {
  if (!ts) return '';
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 3600)  return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

async function loadNews(force) {
  const panel = document.getElementById('news-panel');
  const body  = document.getElementById('news-body');
  const count = document.getElementById('news-count');
  const btn   = document.getElementById('btn-news-refresh');
  if (!force && _newsLoaded) return;
  if (btn) btn.disabled = true;
  try {
    const res  = await fetch('/api/news');
    const data = await res.json();
    if (!data.ok || !data.items || !data.items.length) {
      body.innerHTML = '<div class="news-loading">No headlines available — watchlist may be empty or market closed.</div>';
      panel.style.display = '';
      return;
    }
    _newsLoaded = true;
    panel.style.display = '';
    count.textContent = data.items.length;
    body.innerHTML = data.items.map(n => {
      const urlAttr = n.url ? `href="${n.url}" target="_blank" rel="noopener"` : '';
      const time = _relTime(n.published_at);
      return `<div class="news-item">
        <div style="display:flex;align-items:baseline;flex-wrap:wrap;gap:4px">
          <span class="news-sym">${n.symbol}</span>
          <a class="news-title" ${urlAttr}>${n.title || '(no title)'}</a>
        </div>
        <div class="news-meta">${n.publisher || ''}${n.publisher && time ? ' · ' : ''}${time}</div>
      </div>`;
    }).join('');
  } catch(e) {
    body.innerHTML = '<div class="news-loading" style="color:#f87171">News fetch failed: ' + e + '</div>';
    panel.style.display = '';
  } finally {
    if (btn) btn.disabled = false;
  }
}
// Load news once on init; auto-refresh every 15 min
loadNews(false);
setInterval(() => loadNews(true), 900000);

function renderChart(d) {
  // Volume bar colours: spike = orange, normal = slate
  const volColors = (d.vol_ratio || []).map(vr =>
    vr >= 3 ? '#f97316' : vr >= 2 ? '#fb923c' : vr >= 1.5 ? '#fbbf24' : '#334155'
  );

  const traces = [
    // Bollinger Band fill (upper first, lower fills to it)
    {type:'scatter',mode:'lines',x:d.dates,y:d.bb_upper,
     line:{color:'rgba(96,165,250,0.3)',width:1},xaxis:'x',yaxis:'y',
     name:'BB Upper',showlegend:false},
    {type:'scatter',mode:'lines',x:d.dates,y:d.bb_lower,
     line:{color:'rgba(96,165,250,0.3)',width:1},
     fill:'tonexty',fillcolor:'rgba(96,165,250,0.05)',
     xaxis:'x',yaxis:'y',name:'Bollinger Bands'},
    // Candlestick
    {type:'candlestick',x:d.dates,open:d.open,high:d.high,low:d.low,close:d.close,
     name:d.symbol,
     increasing:{line:{color:'#22c55e'},fillcolor:'#14532d'},
     decreasing:{line:{color:'#ef4444'},fillcolor:'#7f1d1d'},
     xaxis:'x',yaxis:'y'},
    // EMAs
    {type:'scatter',mode:'lines',x:d.dates,y:d.ema_fast,
     name:'EMA '+d.ema_fast_period,line:{color:'#f97316',width:1.5},xaxis:'x',yaxis:'y'},
    {type:'scatter',mode:'lines',x:d.dates,y:d.ema_slow,
     name:'EMA '+d.ema_slow_period,line:{color:'#8b5cf6',width:1.5},xaxis:'x',yaxis:'y'},
    // RSI subplot
    {type:'scatter',mode:'lines',x:d.dates,y:d.rsi,name:'RSI',
     line:{color:'#f59e0b',width:1.5},xaxis:'x',yaxis:'y2'},
    // Volume bars (coloured by spike ratio)
    {type:'bar',x:d.dates,y:d.volume,name:'Volume',
     marker:{color:volColors,opacity:0.7},
     xaxis:'x',yaxis:'y3',showlegend:false},
  ];

  // Base shapes: RSI reference lines
  const shapes = [
    {type:'line',xref:'paper',x0:0,x1:1,y0:70,y1:70,yref:'y2',
     line:{color:'rgba(239,68,68,0.45)',width:1,dash:'dot'}},
    {type:'line',xref:'paper',x0:0,x1:1,y0:30,y1:30,yref:'y2',
     line:{color:'rgba(34,197,94,0.45)',width:1,dash:'dot'}},
  ];
  const annotations = [
    {text:'RSI',x:0.004,xref:'paper',y:0.205,yref:'paper',
     showarrow:false,font:{color:'#f59e0b',size:10}},
    {text:'Vol',x:0.004,xref:'paper',y:0.055,yref:'paper',
     showarrow:false,font:{color:'#64748b',size:10}},
  ];

  // Support / resistance horizontal lines + price labels
  (d.sr_levels || []).forEach(lvl => {
    const isRes = lvl.type === 'resistance';
    const col   = isRes ? 'rgba(239,68,68,0.6)' : 'rgba(34,197,94,0.6)';
    const dash  = isRes ? 'dash' : 'dot';
    shapes.push({
      type:'line', xref:'paper', x0:0, x1:1,
      y0:lvl.price, y1:lvl.price, yref:'y',
      line:{color:col, width:1, dash:dash},
    });
    annotations.push({
      x:1, xref:'paper', y:lvl.price, yref:'y',
      text:'$'+lvl.price.toFixed(2),
      showarrow:false,
      font:{color:col, size:9},
      xanchor:'right',
      bgcolor:'rgba(15,23,42,0.75)',
      borderpad:2,
    });
  });

  const layout = {
    paper_bgcolor:'#0f172a', plot_bgcolor:'#0f172a',
    font:{color:'#94a3b8',family:'Segoe UI,system-ui,sans-serif',size:11},
    margin:{l:55,r:72,t:12,b:40},
    xaxis:{type:'date',rangeslider:{visible:false},gridcolor:'#1e293b',
           tickfont:{color:'#475569',size:10},showgrid:true},
    yaxis:{domain:[0.38,1],gridcolor:'#1e293b',tickfont:{color:'#475569',size:10},
           tickprefix:'$',showgrid:true},
    yaxis2:{domain:[0.2,0.34],gridcolor:'#1e293b',tickfont:{color:'#475569',size:10},
            range:[0,100],showgrid:false},
    yaxis3:{domain:[0,0.16],gridcolor:'#1e293b',tickfont:{color:'#475569',size:10},
            showgrid:false,showticklabels:false},
    legend:{orientation:'h',x:0,y:1.06,font:{size:10,color:'#94a3b8'},
            bgcolor:'rgba(0,0,0,0)'},
    shapes,
    annotations,
    bargap:0.1,
  };
  Plotly.newPlot('chart-plotly', traces, layout, {
    responsive:true, displayModeBar:true,
    modeBarButtonsToRemove:['lasso2d','select2d','toggleSpikelines'],
    displaylogo:false,
  });
}

// ── Service Worker (PWA) ──────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>"""


LEADERBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Performance Leaderboard — NYSE Trading Engine</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090f;--surface:#0d1220;--surface2:#121a2e;
  --border:#1a2540;--border2:#223060;
  --accent:#2563eb;--accent2:#3b82f6;
  --green:#10b981;--green2:#34d399;
  --red:#ef4444;--red2:#f87171;
  --amber:#f59e0b;
  --text:#eaf0fb;--text2:#8898b8;--text3:#4a5a78;
}
body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;
     -webkit-font-smoothing:antialiased;min-height:100vh}
a{color:inherit;text-decoration:none}
/* Nav */
nav{display:flex;align-items:center;justify-content:space-between;padding:0 40px;height:60px;
    background:rgba(7,9,15,.96);border-bottom:1px solid var(--border);
    position:sticky;top:0;z-index:20;backdrop-filter:blur(16px)}
.nav-brand{display:flex;align-items:center;gap:10px}
.nav-dot{width:8px;height:8px;background:var(--accent2);border-radius:50%;box-shadow:0 0 10px var(--accent2)}
.nav-name{font-size:15px;font-weight:700;color:#f1f5f9;letter-spacing:-.3px}
.nav-right{display:flex;align-items:center;gap:10px}
.btn-nav{padding:7px 18px;border-radius:7px;background:var(--accent);color:#fff;
         font-size:13px;font-weight:700;text-decoration:none;transition:all .15s}
.btn-nav:hover{background:var(--accent2)}
.btn-nav-ghost{padding:7px 16px;border-radius:7px;border:1px solid var(--border);
               color:var(--text2);font-size:13px;font-weight:600;text-decoration:none;transition:all .15s}
.btn-nav-ghost:hover{border-color:var(--accent2);color:var(--text)}
/* Page layout */
.page{max-width:1100px;margin:0 auto;padding:48px 24px 80px}
/* Header */
.lb-header{text-align:center;margin-bottom:52px}
.lb-eyebrow{display:inline-flex;align-items:center;gap:7px;padding:5px 14px;border-radius:99px;
            background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);
            font-size:11px;font-weight:700;color:var(--green2);letter-spacing:.6px;
            text-transform:uppercase;margin-bottom:20px}
.lb-eyebrow-dot{width:6px;height:6px;background:var(--green2);border-radius:50%;
                box-shadow:0 0 6px var(--green2)}
.lb-title{font-size:clamp(28px,4vw,42px);font-weight:800;letter-spacing:-1.5px;margin-bottom:10px}
.lb-sub{font-size:15px;color:var(--text2);max-width:480px;margin:0 auto}
/* Stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:40px}
@media(max-width:640px){.stats-grid{grid-template-columns:repeat(2,1fr)}}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:22px 20px}
.sc-label{font-size:11px;font-weight:700;color:var(--text3);letter-spacing:.5px;
          text-transform:uppercase;margin-bottom:8px}
.sc-value{font-size:28px;font-weight:800;letter-spacing:-1px;line-height:1}
.sc-sub{font-size:12px;color:var(--text2);margin-top:4px}
.sc-pos{color:var(--green2)}
.sc-neg{color:var(--red2)}
.sc-neu{color:var(--text)}
.sc-blue{color:var(--accent2)}
/* Chart */
.chart-wrap{background:var(--surface);border:1px solid var(--border);border-radius:14px;
            padding:24px;margin-bottom:40px}
.chart-title{font-size:14px;font-weight:700;color:var(--text2);margin-bottom:20px;
             letter-spacing:.3px;text-transform:uppercase;font-size:11px}
.chart-canvas-wrap{position:relative;height:240px}
.chart-empty{display:flex;align-items:center;justify-content:center;height:180px;
             color:var(--text3);font-size:14px}
/* Trades table */
.trades-wrap{background:var(--surface);border:1px solid var(--border);border-radius:14px;
             overflow:hidden;margin-bottom:32px}
.trades-hdr{padding:16px 20px;font-size:11px;font-weight:700;color:var(--text2);
            background:var(--surface2);border-bottom:1px solid var(--border);
            letter-spacing:.5px;text-transform:uppercase}
table{width:100%;border-collapse:collapse}
th{padding:11px 16px;font-size:11px;font-weight:700;color:var(--text3);
   text-align:left;border-bottom:1px solid var(--border);letter-spacing:.4px;text-transform:uppercase}
td{padding:12px 16px;font-size:13px;border-bottom:1px solid rgba(26,37,64,.45);color:var(--text2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(18,26,46,.6)}
.badge{display:inline-block;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700;letter-spacing:.4px}
.badge-buy{background:rgba(16,185,129,.15);color:var(--green2);border:1px solid rgba(16,185,129,.3)}
.badge-sell{background:rgba(239,68,68,.12);color:var(--red2);border:1px solid rgba(239,68,68,.25)}
.pnl-pos{color:var(--green2);font-weight:700}
.pnl-neg{color:var(--red2);font-weight:700}
.sym{color:var(--text);font-weight:700}
.empty-row td{text-align:center;color:var(--text3);padding:32px;border-bottom:none}
/* Disclaimer */
.disclaimer{background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);
            border-radius:10px;padding:16px 20px;font-size:12px;color:#92693b;
            line-height:1.6;text-align:center}
.disc-icon{font-size:16px;margin-right:6px}
/* Loading */
.loading{text-align:center;padding:60px 0;color:var(--text3);font-size:14px}
</style>
</head>
<body>

<nav>
  <div class="nav-brand">
    <div class="nav-dot"></div>
    <span class="nav-name">NYSE Trading Engine</span>
  </div>
  <div class="nav-right">
    <a href="/" class="btn-nav-ghost">Home</a>
    <a href="/login" class="btn-nav">Dashboard &rarr;</a>
  </div>
</nav>

<div class="page">
  <div class="lb-header">
    <div class="lb-eyebrow"><span class="lb-eyebrow-dot"></span>Live Track Record</div>
    <div class="lb-title">Performance Leaderboard</div>
    <div class="lb-sub">Real-time results from the paper trading engine. Updated after every completed trade.</div>
  </div>

  <div id="loading" class="loading">Loading performance data…</div>

  <div id="content" style="display:none">
    <!-- Stats -->
    <div class="stats-grid" id="stats-grid"></div>

    <!-- Chart -->
    <div class="chart-wrap">
      <div class="chart-title">Cumulative Returns Over Time (%)</div>
      <div class="chart-canvas-wrap" id="chart-wrap">
        <canvas id="perf-chart"></canvas>
      </div>
    </div>

    <!-- Trades table -->
    <div class="trades-wrap">
      <div class="trades-hdr">Last 20 Completed Trades</div>
      <table>
        <thead>
          <tr>
            <th>Date</th>
            <th>Ticker</th>
            <th>Action</th>
            <th>Entry Price</th>
            <th>Exit Price</th>
            <th>Hold (days)</th>
            <th>P&amp;L %</th>
          </tr>
        </thead>
        <tbody id="trades-body"></tbody>
      </table>
    </div>

    <!-- Disclaimer -->
    <div class="disclaimer">
      <span class="disc-icon">&#9888;</span>
      <strong>Past performance does not guarantee future results.</strong>
      This is a paper trading account for demonstration purposes only. All trades are simulated using real market prices but no real money is at risk. Not financial advice.
    </div>
  </div>
</div>

<script>
function fmt(v, dec=2) {
  if (v == null) return "—";
  const s = v > 0 ? "+" : "";
  return s + v.toFixed(dec) + "%";
}
function fmtDays(d) {
  if (d == null) return "—";
  if (d === 0) return "< 1d";
  return d + "d";
}
function fmtPrice(v) {
  if (v == null) return "—";
  return "$" + v.toFixed(2);
}
function fmtDate(s) {
  if (!s) return "—";
  return s.slice(0, 10);
}

async function load() {
  try {
    const res  = await fetch("/api/leaderboard");
    const data = await res.json();
    if (!data.ok) throw new Error(data.error);
    render(data);
  } catch(e) {
    document.getElementById("loading").textContent = "Unable to load data: " + e.message;
  }
}

function render({ stats, trades, chart }) {
  document.getElementById("loading").style.display = "none";
  document.getElementById("content").style.display = "block";
  renderStats(stats);
  renderChart(chart);
  renderTrades(trades);
}

function renderStats(s) {
  const hasTrades = s.total_trades > 0;
  const totalRetClass = !hasTrades ? "sc-neu" : s.total_return_pct >= 0 ? "sc-pos" : "sc-neg";
  const cards = [
    { label:"Total Return", value: hasTrades ? fmt(s.total_return_pct) : "—", cls: totalRetClass, sub: "Cumulative trade returns" },
    { label:"Win Rate",     value: hasTrades ? s.win_rate + "%" : "—", cls: "sc-blue", sub: `${s.winners || 0} wins / ${s.total_trades} trades` },
    { label:"Total Trades", value: s.total_trades, cls: "sc-neu", sub: "Completed buy-sell cycles" },
    { label:"Best Trade",   value: hasTrades ? fmt(s.best_trade_pct) : "—", cls: "sc-pos", sub: s.best_symbol || "" },
    { label:"Worst Trade",  value: hasTrades ? fmt(s.worst_trade_pct) : "—", cls: "sc-neg", sub: s.worst_symbol || "" },
    { label:"Avg Hold",     value: hasTrades && s.avg_hold_days != null ? fmtDays(s.avg_hold_days) : "—", cls: "sc-neu", sub: "Average days per trade" },
  ];
  document.getElementById("stats-grid").innerHTML = cards.map(c =>
    `<div class="stat-card">
      <div class="sc-label">${c.label}</div>
      <div class="sc-value ${c.cls}">${c.value}</div>
      <div class="sc-sub">${c.sub}</div>
    </div>`
  ).join("");
}

function renderChart(pts) {
  if (!pts || pts.length === 0) {
    document.getElementById("chart-wrap").innerHTML =
      '<div class="chart-empty">No completed trades yet — chart will appear here.</div>';
    return;
  }
  const labels = pts.map(p => p.ts);
  const values = pts.map(p => p.value);
  const color  = values[values.length - 1] >= 0 ? "#10b981" : "#ef4444";
  new Chart(document.getElementById("perf-chart"), {
    type: "line",
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: color,
        borderWidth: 2,
        pointRadius: pts.length < 40 ? 3 : 0,
        pointHoverRadius: 5,
        pointBackgroundColor: color,
        fill: true,
        backgroundColor: (ctx) => {
          const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height);
          g.addColorStop(0, color + "26");
          g.addColorStop(1, color + "04");
          return g;
        },
        tension: 0.3,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => (ctx.parsed.y >= 0 ? "+" : "") + ctx.parsed.y.toFixed(2) + "%",
          },
        },
      },
      scales: {
        x: { ticks: { color: "#4a5a78", maxTicksLimit: 8, font: { size: 11 } }, grid: { color: "#1a254010" } },
        y: {
          ticks: { color: "#4a5a78", font: { size: 11 }, callback: v => v + "%" },
          grid: { color: "#1a2540" },
        },
      },
    },
  });
}

function renderTrades(trades) {
  const tbody = document.getElementById("trades-body");
  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No completed trades yet.</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlClass = t.pnl_pct >= 0 ? "pnl-pos" : "pnl-neg";
    const sign = t.pnl_pct >= 0 ? "+" : "";
    return `<tr>
      <td>${fmtDate(t.date)}</td>
      <td class="sym">${t.symbol}</td>
      <td><span class="badge badge-sell">SELL</span></td>
      <td>${fmtPrice(t.entry_price)}</td>
      <td>${fmtPrice(t.exit_price)}</td>
      <td>${fmtDays(t.hold_days)}</td>
      <td class="${pnlClass}">${sign}${t.pnl_pct.toFixed(2)}%</td>
    </tr>`;
  }).join("");
}

load();
</script>
</body>
</html>"""


SETTINGS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Settings — NYSE Trading Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090f;--surface:#0d1220;--surface2:#121a2e;
  --border:#1a2540;--border2:#223060;
  --accent:#2563eb;--accent2:#3b82f6;
  --green:#10b981;--amber:#f59e0b;
  --text:#eaf0fb;--text2:#8898b8;--text3:#4a5a78;
}
body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;
     -webkit-font-smoothing:antialiased;min-height:100vh}
a{color:inherit;text-decoration:none}
/* Nav */
nav{display:flex;align-items:center;justify-content:space-between;padding:0 32px;height:56px;
    background:rgba(13,18,32,.98);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
.nav-left{display:flex;align-items:center;gap:18px}
.nav-logo{font-size:14px;font-weight:700;color:#f1f5f9;letter-spacing:-.3px}
.nav-dot{width:7px;height:7px;background:var(--accent2);border-radius:50%;box-shadow:0 0 8px var(--accent2)}
.nav-link{font-size:13px;color:var(--text2);padding:5px 10px;border-radius:6px;transition:all .15s}
.nav-link:hover{background:var(--surface2);color:var(--text)}
.nav-link.active{background:var(--surface2);color:var(--text)}
.btn-logout{padding:6px 14px;border-radius:6px;background:#7f1d1d;color:#fca5a5;font-size:12px;
            font-weight:600;border:1px solid #991b1b}
/* Page */
.page{max-width:860px;margin:0 auto;padding:48px 24px}
.page-title{font-size:26px;font-weight:700;letter-spacing:-.5px;margin-bottom:6px}
.page-sub{font-size:14px;color:var(--text2);margin-bottom:40px}
/* Profile cards */
.profiles{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:40px}
@media(max-width:640px){.profiles{grid-template-columns:1fr}}
.profile-card{background:var(--surface);border:2px solid var(--border);border-radius:14px;
              padding:26px 24px;cursor:pointer;transition:all .2s;position:relative;user-select:none}
.profile-card:hover{border-color:var(--border2);background:var(--surface2);transform:translateY(-2px)}
.profile-card.selected{border-color:var(--card-color,var(--accent));
                        box-shadow:0 0 0 1px var(--card-color,var(--accent)),
                                   0 0 24px color-mix(in srgb,var(--card-color,var(--accent)) 20%,transparent)}
.profile-icon{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;
              font-size:22px;margin-bottom:18px;background:color-mix(in srgb,var(--card-color,var(--accent)) 15%,transparent)}
.profile-name{font-size:17px;font-weight:700;margin-bottom:6px;color:var(--text)}
.profile-tagline{font-size:12px;color:var(--text2);margin-bottom:18px;line-height:1.5}
.profile-params{display:flex;flex-direction:column;gap:7px}
.param-row{display:flex;justify-content:space-between;align-items:center;font-size:12px}
.param-label{color:var(--text3)}
.param-val{color:var(--text2);font-weight:600}
.selected-badge{position:absolute;top:14px;right:14px;background:var(--card-color,var(--accent));
                color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:20px;
                letter-spacing:.5px;display:none}
.profile-card.selected .selected-badge{display:block}
/* Save button */
.save-row{display:flex;align-items:center;gap:14px;margin-bottom:40px}
.btn-save{padding:11px 32px;background:var(--accent);color:#fff;border:none;border-radius:8px;
          font-size:14px;font-weight:700;cursor:pointer;transition:all .15s}
.btn-save:hover{background:var(--accent2)}
.btn-save:disabled{opacity:.5;cursor:default}
.save-msg{font-size:13px;color:var(--green);display:none}
/* Detail table */
.detail-wrap{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.detail-hdr{padding:14px 20px;font-size:13px;font-weight:700;color:var(--text2);
            background:var(--surface2);border-bottom:1px solid var(--border);letter-spacing:.4px}
.detail-table{width:100%;border-collapse:collapse}
.detail-table th,.detail-table td{padding:11px 20px;font-size:13px;text-align:left}
.detail-table th{color:var(--text3);font-weight:600;border-bottom:1px solid var(--border)}
.detail-table td{border-bottom:1px solid rgba(26,37,64,.5)}
.detail-table tr:last-child td{border-bottom:none}
.detail-table td:last-child{text-align:right;font-weight:600;color:var(--text)}
td.diff-up{color:#6ee7b7}
td.diff-dn{color:#f87171}
/* Email section */
.section-title{font-size:18px;font-weight:700;margin:40px 0 6px;letter-spacing:-.3px}
.section-sub{font-size:13px;color:var(--text2);margin-bottom:20px}
.email-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;
            display:flex;align-items:center;justify-content:space-between;gap:16px}
.email-info{flex:1}
.email-title{font-size:15px;font-weight:700;margin-bottom:4px}
.email-desc{font-size:12px;color:var(--text2);line-height:1.5}
.email-unconfigured{font-size:12px;color:#f59e0b;margin-top:8px;padding:8px 12px;
                     background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25);
                     border-radius:6px;display:inline-flex;align-items:center;gap:6px}
/* Alpaca key form */
.alpaca-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:24px;margin-bottom:16px}
.alpaca-row{display:flex;flex-direction:column;gap:6px;margin-bottom:16px}
.alpaca-label{font-size:12px;color:var(--text2);font-weight:600}
.alpaca-input{background:#07090f;border:1px solid var(--border2);border-radius:8px;padding:10px 14px;
              color:var(--text);font-size:13px;font-family:monospace;width:100%;outline:none}
.alpaca-input:focus{border-color:var(--accent)}
.alpaca-mode{display:flex;gap:12px;margin-bottom:16px}
.alpaca-mode label{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text2);cursor:pointer}
.alpaca-status{font-size:12px;margin-top:12px;padding:8px 12px;border-radius:6px;display:none}
.alpaca-status.ok{background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);color:#34d399}
.alpaca-status.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#f87171}
/* Toggle switch */
.toggle-wrap{display:flex;align-items:center;gap:10px;flex-shrink:0}
.toggle-label{font-size:12px;color:var(--text2);min-width:36px;text-align:right}
.toggle{position:relative;width:48px;height:26px;flex-shrink:0}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle-slider{position:absolute;inset:0;background:#1a2540;border-radius:26px;
               cursor:pointer;transition:background .2s}
.toggle-slider:before{content:"";position:absolute;width:20px;height:20px;left:3px;top:3px;
                       background:#4a5a78;border-radius:50%;transition:all .2s}
.toggle input:checked ~ .toggle-slider{background:#10b981}
.toggle input:checked ~ .toggle-slider:before{transform:translateX(22px);background:#fff}
.toggle input:disabled ~ .toggle-slider{opacity:.4;cursor:not-allowed}
</style>
</head>
<body>
<nav>
  <div class="nav-left">
    <div class="nav-dot"></div>
    <span class="nav-logo">NYSE Engine</span>
    <a href="/dashboard" class="nav-link">Dashboard</a>
    <a href="/journal" class="nav-link">Journal</a>
    <a href="/settings" class="nav-link active">Settings</a>
  </div>
  <div>
    <a href="/logout" class="btn-logout">Logout</a>
  </div>
</nav>

<div class="page">
  <div class="page-title">Settings</div>
  <div class="page-sub">Choose your risk profile. Changes apply immediately to the live trading engine.</div>

  <div class="profiles" id="profiles">
    <!-- Rendered by JS -->
  </div>

  <div class="save-row">
    <button class="btn-save" id="btn-save" onclick="saveProfile()">Save Profile</button>
    <span class="save-msg" id="save-msg">&#10003; Saved successfully</span>
  </div>

  <div class="detail-wrap">
    <div class="detail-hdr">PROFILE COMPARISON</div>
    <table class="detail-table">
      <thead>
        <tr>
          <th>Parameter</th>
          <th>Conservative</th>
          <th>Moderate</th>
          <th>Aggressive</th>
        </tr>
      </thead>
      <tbody id="detail-body">
      </tbody>
    </table>
  </div>

  <!-- Alpaca API Keys -->
  <div class="section-title">Alpaca API Connection</div>
  <div class="section-sub">Enter your Alpaca API credentials to enable live or paper trading. Keys are encrypted and stored per account.</div>
  <div class="alpaca-card">
    <div class="alpaca-row">
      <span class="alpaca-label">API Key</span>
      <input class="alpaca-input" id="alpaca-api-key" type="password" placeholder="PK…" autocomplete="off" spellcheck="false"/>
    </div>
    <div class="alpaca-row">
      <span class="alpaca-label">Secret Key</span>
      <input class="alpaca-input" id="alpaca-secret-key" type="password" placeholder="secret…" autocomplete="off" spellcheck="false"/>
    </div>
    <div class="alpaca-mode">
      <label><input type="radio" name="alpaca-mode" value="paper" {% if alpaca_paper %}checked{% endif %}/> Paper Trading</label>
      <label><input type="radio" name="alpaca-mode" value="live"  {% if not alpaca_paper %}checked{% endif %}/> Live Trading</label>
    </div>
    <button class="btn-save" onclick="saveAlpacaKeys()">Save Keys</button>
    {% if alpaca_connected %}
    <span style="font-size:12px;color:#10b981;margin-left:14px">&#10003; Connected ({{ "Paper" if alpaca_paper else "Live" }})</span>
    {% endif %}
    <div class="alpaca-status" id="alpaca-status"></div>
  </div>

  <!-- Email Notifications -->
  <div class="section-title">Notifications</div>
  <div class="section-sub">Get an email whenever a trade is executed with the ticker, action, price, score, and a plain-English explanation.</div>
  <div class="email-card" id="email-card">
    <div class="email-info">
      <div class="email-title">Email Notifications</div>
      <div class="email-desc">Sends a trade alert to <strong id="notify-addr">{{ notify_email or "NOTIFY_EMAIL" }}</strong> when a BUY or SELL is executed.</div>
      {% if not email_configured %}
      <div class="email-unconfigured">
        &#9888; Add <code>EMAIL_HOST</code>, <code>EMAIL_PORT</code>, <code>EMAIL_USER</code>, <code>EMAIL_PASSWORD</code>, and <code>NOTIFY_EMAIL</code> environment variables to enable notifications.
      </div>
      {% endif %}
    </div>
    <div class="toggle-wrap">
      <span class="toggle-label" id="email-state-label">{{ "ON" if email_active else "OFF" }}</span>
      <label class="toggle">
        <input type="checkbox" id="email-toggle"
               {% if email_active %}checked{% endif %}
               {% if not email_configured %}disabled{% endif %}
               onchange="toggleEmail(this.checked)">
        <span class="toggle-slider"></span>
      </label>
    </div>
  </div>
</div>

<script>
const PROFILES = {{ profiles_json | safe }};
let selected = "{{ current_profile }}";


const ICONS = { conservative: "🛡️", moderate: "⚖️", aggressive: "🚀" };

function pct(v) {
  if (typeof v === "number" && Math.abs(v) < 10) return (v * 100).toFixed(0) + "%";
  return v;
}

function renderCards() {
  const container = document.getElementById("profiles");
  container.innerHTML = "";
  for (const [key, prof] of Object.entries(PROFILES)) {
    const card = document.createElement("div");
    card.className = "profile-card" + (key === selected ? " selected" : "");
    card.style.setProperty("--card-color", prof.color);
    const overrides = prof.overrides;
    card.innerHTML = `
      <span class="selected-badge">ACTIVE</span>
      <div class="profile-icon">${ICONS[key] || "📊"}</div>
      <div class="profile-name">${prof.label}</div>
      <div class="profile-tagline">${prof.tagline}</div>
      <div class="profile-params">
        <div class="param-row"><span class="param-label">Position size</span><span class="param-val">${pct(overrides.max_position_pct)}</span></div>
        <div class="param-row"><span class="param-label">Stop-loss</span><span class="param-val">${pct(overrides.stop_loss_pct)}</span></div>
        <div class="param-row"><span class="param-label">Take-profit</span><span class="param-val">${pct(overrides.take_profit_pct)}</span></div>
        <div class="param-row"><span class="param-label">Min score</span><span class="param-val">${prof.score_label || overrides.buy_threshold}</span></div>
        <div class="param-row"><span class="param-label">Max positions</span><span class="param-val">${overrides.max_open_positions}</span></div>
      </div>`;
    card.onclick = () => selectProfile(key);
    container.appendChild(card);
  }
}

function renderDetail() {
  const tbody = document.getElementById("detail-body");
  const rows = [
    {label: "Position Size",       c: "2%",     m: "5%",   a: "10%"},
    {label: "Stop-Loss",           c: "3%",     m: "5%",   a: "8%"},
    {label: "Signal Threshold",    c: "8+",     m: "6+",   a: "4+"},
    {label: "Max Open Positions",  c: "3",      m: "5",    a: "8"},
    {label: "Rebalance Frequency", c: "Weekly", m: "Daily",a: "Every Cycle"},
  ];
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${r.label}</td>
    <td class="diff-up">${r.c}</td>
    <td>${r.m}</td>
    <td class="diff-dn">${r.a}</td>
  </tr>`).join("");
}

function selectProfile(key) {
  selected = key;
  renderCards();
  document.getElementById("save-msg").style.display = "none";
}

async function saveProfile() {
  const btn = document.getElementById("btn-save");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ risk_profile: selected }),
    });
    const data = await res.json();
    if (data.ok) {
      const msg = document.getElementById("save-msg");
      msg.style.display = "inline";
      setTimeout(() => { msg.style.display = "none"; }, 3000);
    } else {
      alert("Save failed: " + (data.error || "unknown error"));
    }
  } catch(e) {
    alert("Network error: " + e);
  } finally {
    btn.disabled = false;
    btn.textContent = "Save Profile";
  }
}

async function toggleEmail(enabled) {
  const toggle = document.getElementById("email-toggle");
  const label  = document.getElementById("email-state-label");
  toggle.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email_notifications: enabled }),
    });
    const data = await res.json();
    if (data.ok) {
      label.textContent = enabled ? "ON" : "OFF";
    } else {
      toggle.checked = !enabled;
      label.textContent = !enabled ? "ON" : "OFF";
      alert(data.error || "Failed to save setting");
    }
  } catch(e) {
    toggle.checked = !enabled;
    label.textContent = !enabled ? "ON" : "OFF";
    alert("Network error: " + e);
  } finally {
    toggle.disabled = false;
  }
}

renderCards();
renderDetail();

async function saveAlpacaKeys() {
  const apiKey    = document.getElementById('alpaca-api-key').value.trim();
  const secretKey = document.getElementById('alpaca-secret-key').value.trim();
  const modeEl    = document.querySelector('input[name="alpaca-mode"]:checked');
  const paper     = modeEl ? modeEl.value === 'paper' : true;
  const status    = document.getElementById('alpaca-status');
  if (!apiKey || !secretKey) {
    status.textContent = 'Both API Key and Secret Key are required.';
    status.className = 'alpaca-status err';
    status.style.display = 'block';
    return;
  }
  try {
    const res  = await fetch('/api/alpaca-keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({api_key: apiKey, secret_key: secretKey, paper: paper}),
    });
    const data = await res.json();
    if (data.ok) {
      status.textContent = '✓ Keys saved. Your trading engine will use these credentials.';
      status.className = 'alpaca-status ok';
      document.getElementById('alpaca-api-key').value = '';
      document.getElementById('alpaca-secret-key').value = '';
    } else {
      status.textContent = 'Error: ' + (data.error || 'unknown error');
      status.className = 'alpaca-status err';
    }
  } catch(e) {
    status.textContent = 'Network error: ' + e;
    status.className = 'alpaca-status err';
  }
  status.style.display = 'block';
}
</script>
</body>
</html>"""


JOURNAL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Trade Journal — NYSE Trading Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090f;--surface:#0d1220;--surface2:#121a2e;
  --border:#1a2540;--border2:#223060;
  --accent:#2563eb;--accent2:#3b82f6;
  --green:#10b981;--green2:#34d399;
  --red:#ef4444;--red2:#f87171;
  --text:#eaf0fb;--text2:#8898b8;--text3:#4a5a78;
}
body{background:var(--bg);color:var(--text);font-family:'Inter','Segoe UI',system-ui,sans-serif;
     font-size:14px;min-height:100vh;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
/* ── Header ── */
header{background:rgba(7,9,15,.95);border-bottom:1px solid var(--border);
       padding:0 24px;height:56px;display:flex;align-items:center;gap:14px;
       position:sticky;top:0;z-index:10;backdrop-filter:blur(12px)}
.logo{font-size:15px;font-weight:700;color:#f1f5f9;letter-spacing:-.3px;display:flex;align-items:center;gap:7px}
.logo::before{content:'';display:inline-block;width:7px;height:7px;background:var(--accent2);
              border-radius:50%;box-shadow:0 0 8px var(--accent2)}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.btn-back{background:var(--surface2);color:var(--text2);border:1px solid var(--border);
          border-radius:6px;padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer}
.btn-back:hover{border-color:var(--accent2);color:var(--text)}
/* ── Layout ── */
main{padding:24px;max-width:1400px;margin:0 auto}
/* ── Stat cards ── */
.stat-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.sc{background:var(--surface);border-radius:10px;padding:16px 18px;border:1px solid var(--border);
    box-shadow:0 2px 8px rgba(0,0,0,.3)}
.sc-label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:7px;font-weight:500}
.sc-val{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.5px}
.sc-sub{font-size:11px;color:var(--text3);margin-top:3px}
.pos{color:var(--green2)}.neg{color:var(--red2)}.neu{color:var(--text)}
/* ── Panel ── */
.panel{background:var(--surface);border-radius:12px;border:1px solid var(--border);
       overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.3)}
.panel-hdr{padding:13px 18px;display:flex;align-items:center;justify-content:space-between;
           border-bottom:1px solid var(--border);background:var(--surface2);flex-wrap:wrap;gap:8px}
.panel-title{font-weight:700;font-size:12px;color:var(--text2);text-transform:uppercase;
             letter-spacing:.6px;display:flex;align-items:center;gap:8px}
.count-badge{background:var(--surface);color:var(--text3);border:1px solid var(--border);
             border-radius:99px;padding:1px 9px;font-size:11px}
.filter-wrap{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.filter-btn{padding:4px 12px;border-radius:99px;border:1px solid var(--border);background:none;
            color:var(--text3);font-size:11px;font-weight:600;cursor:pointer}
.filter-btn.active{background:var(--surface2);color:var(--text);border-color:var(--border2)}
.filter-btn:hover{border-color:var(--accent2);color:var(--text)}
/* ── Table ── */
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;min-width:700px}
th{padding:10px 14px;text-align:left;font-size:10px;color:var(--text3);text-transform:uppercase;
   letter-spacing:.6px;border-bottom:1px solid var(--border);white-space:nowrap;font-weight:700;
   background:var(--surface2)}
td{padding:11px 14px;border-bottom:1px solid rgba(26,37,64,.6);font-variant-numeric:tabular-nums;
   font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(18,26,46,.8)}
/* ── Pills ── */
.pill{display:inline-block;padding:3px 9px;border-radius:4px;font-weight:700;font-size:11px;letter-spacing:.3px}
.pill-BUY{background:rgba(16,185,129,.15);color:var(--green2);border:1px solid rgba(16,185,129,.2)}
.pill-SELL{background:rgba(239,68,68,.15);color:var(--red2);border:1px solid rgba(239,68,68,.2)}
/* ── Score chip ── */
.score-chip{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:12px;
            font-variant-numeric:tabular-nums}
/* ── Reason cell ── */
.reason-cell{max-width:280px;white-space:normal;line-height:1.4;color:var(--text2)}
/* ── P&L cell ── */
.pnl-pos{color:var(--green2);font-weight:700}
.pnl-neg{color:var(--red2);font-weight:700}
/* ── Empty state ── */
.empty{padding:48px;text-align:center;color:var(--text3)}
.empty-icon{font-size:36px;margin-bottom:12px;opacity:.5}
.empty-msg{font-size:15px;font-weight:600;color:var(--text2);margin-bottom:6px}
.empty-sub{font-size:13px;color:var(--text3)}
/* ── RSI mini badge ── */
.rsi-badge{font-size:11px;color:var(--text3);margin-left:4px}
/* ── Loading ── */
.loading{padding:40px;text-align:center;color:var(--text3);font-size:13px}
/* ── Responsive ── */
@media(max-width:900px){main{padding:16px}}
@media(max-width:600px){
  header{padding:0 14px}
  main{padding:10px 12px}
  .stat-row{grid-template-columns:1fr 1fr}
  .sc{padding:12px}
  .sc-val{font-size:18px}
  td,th{padding:8px 10px;font-size:12px}
  .reason-cell{max-width:160px}
}
</style>
</head>
<body>
<header>
  <div class="logo">Trade Journal</div>
  <div class="hdr-right">
    <button class="btn-back" onclick="window.location='/stats'">Stats</button>
    <button class="btn-back" onclick="window.location='/dashboard'">← Dashboard</button>
  </div>
</header>

<main>
  <!-- ── Summary stats ── -->
  <div class="stat-row" id="stat-row">
    <div class="sc"><div class="sc-label">Total Trades</div><div class="sc-val neu" id="s-total">—</div></div>
    <div class="sc"><div class="sc-label">Win Rate</div><div class="sc-val" id="s-winrate">—</div><div class="sc-sub">on closed positions</div></div>
    <div class="sc"><div class="sc-label">Avg Gain</div><div class="sc-val pos" id="s-gain">—</div></div>
    <div class="sc"><div class="sc-label">Avg Loss</div><div class="sc-val neg" id="s-loss">—</div></div>
    <div class="sc"><div class="sc-label">Total Realized P&amp;L</div><div class="sc-val" id="s-pnl">—</div></div>
    <div class="sc"><div class="sc-label">Best Trade</div><div class="sc-val pos" id="s-best">—</div></div>
    <div class="sc"><div class="sc-label">Worst Trade</div><div class="sc-val neg" id="s-worst">—</div></div>
  </div>

  <!-- ── Journal table ── -->
  <div class="panel">
    <div class="panel-hdr">
      <div class="panel-title">
        All Trades
        <span class="count-badge" id="tbl-count">0</span>
      </div>
      <div class="filter-wrap">
        <button class="filter-btn active" onclick="setFilter('ALL',this)">All</button>
        <button class="filter-btn" onclick="setFilter('BUY',this)">BUY</button>
        <button class="filter-btn" onclick="setFilter('SELL',this)">SELL</button>
      </div>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Date &amp; Time</th>
            <th>Ticker</th>
            <th>Action</th>
            <th>Qty</th>
            <th>Price</th>
            <th>Score</th>
            <th>RSI</th>
            <th>P&amp;L</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody id="jrn-body">
          <tr><td colspan="9" class="loading">Loading journal…</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>

<script>
let _allEntries = [];
let _filter = 'ALL';

function fmt$(v, dec=2) {
  if (v == null) return '—';
  return '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits:dec, maximumFractionDigits:dec});
}
function fmtNum(v, dec=2) {
  if (v == null) return '—';
  return Number(v).toFixed(dec);
}
function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const date = d.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'});
  const time = d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit', hour12:true});
  return `<span style="color:#eaf0fb;font-weight:600">${date}</span><br>
          <span style="color:#4a5a78;font-size:11px">${time}</span>`;
}

function setFilter(f, btn) {
  _filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderTable();
}

function renderTable() {
  const rows = _filter === 'ALL' ? _allEntries
             : _allEntries.filter(e => e.action === _filter);
  document.getElementById('tbl-count').textContent = rows.length;
  const tbody = document.getElementById('jrn-body');
  if (!rows.length) {
    const msg = _filter === 'ALL' ? 'No trades logged yet — run a cycle to start trading'
              : `No ${_filter} trades in the journal`;
    tbody.innerHTML = `<tr><td colspan="9">
      <div class="empty">
        <div class="empty-icon">📋</div>
        <div class="empty-msg">No trades yet</div>
        <div class="empty-sub">${msg}</div>
      </div>
    </td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(e => {
    const ind   = e.indicators || {};
    const score = ind.score != null ? ind.score : null;
    const rsi   = ind.rsi   != null ? ind.rsi   : null;
    const isBuy = e.action === 'BUY';

    // Score chip colour
    const scoreCol = score == null ? '#4a5a78'
                   : score >= 0.3 ? '#34d399' : score <= -0.3 ? '#f87171' : '#8898b8';
    const scoreStr = score != null ? `${score >= 0 ? '+' : ''}${fmtNum(score, 3)}` : '—';

    // RSI colour hint
    const rsiCol = rsi == null ? '#4a5a78'
                 : rsi < 30 ? '#34d399' : rsi > 70 ? '#f87171' : '#8898b8';

    // P&L
    let pnlHtml = '<span style="color:#4a5a78">—</span>';
    if (e.pnl != null) {
      const sign = e.pnl >= 0 ? '+' : '';
      const cls  = e.pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
      const pct  = e.pnl_pct != null ? ` (${sign}${(e.pnl_pct*100).toFixed(2)}%)` : '';
      pnlHtml = `<span class="${cls}">${sign}${fmt$(e.pnl)}${pct}</span>`;
    }

    // Reason — truncate long strings with title tooltip
    const reason = e.reason || '—';
    const reasonDisplay = reason.length > 60 ? reason.slice(0, 58) + '…' : reason;

    return `<tr>
      <td>${fmtDate(e.timestamp)}</td>
      <td><span style="font-weight:700;color:#f1f5f9;font-size:14px;letter-spacing:-.3px">${e.symbol}</span></td>
      <td><span class="pill pill-${e.action}">${e.action}</span></td>
      <td style="font-weight:600">${e.shares != null ? fmtNum(e.shares, 0) : '—'}</td>
      <td style="font-weight:600">${fmt$(e.price)}</td>
      <td><span class="score-chip" style="color:${scoreCol};background:${scoreCol}22;border:1px solid ${scoreCol}33">${scoreStr}</span></td>
      <td style="color:${rsiCol}">${rsi != null ? fmtNum(rsi, 1) : '—'}</td>
      <td>${pnlHtml}</td>
      <td class="reason-cell" title="${reason.replace(/"/g,'&quot;')}">${reasonDisplay}</td>
    </tr>`;
  }).join('');
}

function renderStats(stats) {
  const dollar = v => v == null ? '—' : (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2);
  document.getElementById('s-total').textContent   = stats.total_trades ?? '—';
  const wr = document.getElementById('s-winrate');
  wr.textContent = stats.win_rate != null ? stats.win_rate + '%' : '—';
  wr.className   = 'sc-val ' + (stats.win_rate >= 50 ? 'pos' : stats.win_rate < 50 ? 'neg' : 'neu');
  document.getElementById('s-gain').textContent    = stats.avg_gain != null ? '+$' + stats.avg_gain.toFixed(2) : '—';
  document.getElementById('s-loss').textContent    = stats.avg_loss != null ? '$' + stats.avg_loss.toFixed(2) : '—';
  const pnlEl = document.getElementById('s-pnl');
  pnlEl.textContent = dollar(stats.total_pnl);
  pnlEl.className   = 'sc-val ' + (stats.total_pnl > 0 ? 'pos' : stats.total_pnl < 0 ? 'neg' : 'neu');
  document.getElementById('s-best').textContent    = stats.best_trade  != null ? '+$' + stats.best_trade.toFixed(2) : '—';
  document.getElementById('s-worst').textContent   = stats.worst_trade != null ? '$' + stats.worst_trade.toFixed(2) : '—';
}

async function loadJournal() {
  try {
    const res  = await fetch('/api/journal');
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || 'API error');
    // most recent first
    _allEntries = (data.entries || []).slice().reverse();
    renderTable();
    if (data.stats) renderStats(data.stats);
  } catch (err) {
    document.getElementById('jrn-body').innerHTML =
      `<tr><td colspan="9" class="loading" style="color:#f87171">Failed to load: ${err.message}</td></tr>`;
  }
}

loadJournal();
</script>
</body>
</html>"""


STATS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="theme-color" content="#1e293b"/>
<meta name="apple-mobile-web-app-capable" content="yes"/>
<link rel="manifest" href="/manifest.json"/>
<title>Performance — NYSE Trading Engine</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js" charset="utf-8"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;min-height:100vh}
header{background:#1e293b;border-bottom:1px solid #334155;padding:12px 20px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:10}
.logo{font-size:17px;font-weight:700;color:#f1f5f9}
.back{font-size:12px;color:#64748b;cursor:pointer;padding:5px 10px;border-radius:6px;background:#334155;border:none;font-weight:600}
.back:hover{opacity:.8}
main{padding:16px 20px;max-width:1200px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:16px}
.card{background:#1e293b;border-radius:10px;padding:14px 16px;border:1px solid #334155}
.card-label{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.card-value{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.card-sub{font-size:11px;color:#64748b;margin-top:3px}
.pos{color:#22c55e}.neg{color:#ef4444}.neu{color:#e2e8f0}
.panel{background:#1e293b;border-radius:10px;border:1px solid #334155;overflow:hidden;margin-bottom:14px}
.panel-title{padding:11px 16px;font-weight:600;font-size:12px;color:#94a3b8;border-bottom:1px solid #334155;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:8px}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;min-width:400px}
th{padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #334155;white-space:nowrap}
td{padding:8px 12px;border-bottom:1px solid #1e293b;font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:#263044}
.pill{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:700;font-size:11px}
.pill-BUY{background:#14532d;color:#4ade80}
.pill-SELL{background:#7f1d1d;color:#f87171}
.empty{padding:28px;text-align:center;color:#475569}
/* chart */
.chart-wrap{padding:16px;background:#0f172a;min-height:180px;display:flex;align-items:center;justify-content:center}
.chart-empty{color:#475569;font-size:13px}
/* period tabs */
.tab-btn{padding:3px 12px;border-radius:99px;border:1px solid #334155;background:none;color:#64748b;font-size:11px;font-weight:600;cursor:pointer;min-height:24px}
.tab-btn.active{background:#334155;color:#e2e8f0;border-color:#334155}
/* section divider */
.section-label{font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.6px;margin:16px 0 8px;font-weight:600}
/* risk metric cards */
.card-rating{font-size:10px;font-weight:600;padding:2px 7px;border-radius:99px;margin-top:4px;display:inline-block}
.rating-great{background:#14532d;color:#4ade80}
.rating-good{background:#1e3a5f;color:#93c5fd}
.rating-ok{background:#451a03;color:#fdba74}
.rating-bad{background:#7f1d1d;color:#f87171}
.rating-na{background:#1e293b;color:#475569}
/* backtest panel */
.bt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;padding:14px 16px}
.bt-card{background:#0f172a;border-radius:8px;padding:12px 14px;border:1px solid #334155}
.bt-label{font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.bt-value{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.bt-sub{font-size:11px;color:#64748b;margin-top:2px}
.bt-beats{margin:0 16px 14px;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:600}
.bt-beats-yes{background:#0f2318;color:#4ade80;border:1px solid #166534}
.bt-beats-no{background:#1c0a0a;color:#f87171;border:1px solid #7f1d1d}
.bt-run-wrap{padding:12px 16px;border-top:1px solid #334155;display:flex;align-items:center;gap:10px}
/* notification panel */
.notif-row{display:flex;align-items:flex-start;gap:14px;padding:14px 16px;border-bottom:1px solid #334155}
.notif-row:last-child{border-bottom:none}
.notif-icon{font-size:22px;flex-shrink:0;margin-top:2px}
.notif-head{font-weight:600;font-size:13px;margin-bottom:4px}
.notif-body{font-size:12px;color:#94a3b8;line-height:1.6}
.notif-body code{background:#334155;padding:1px 5px;border-radius:3px;font-size:11px;color:#e2e8f0}
.badge-on{display:inline-block;padding:2px 8px;border-radius:99px;background:#14532d;color:#4ade80;font-size:11px;font-weight:600;margin-left:8px}
.badge-off{display:inline-block;padding:2px 8px;border-radius:99px;background:#374151;color:#9ca3af;font-size:11px;font-weight:600;margin-left:8px}
@media(max-width:600px){
  .cards{grid-template-columns:1fr 1fr}
  .card-value{font-size:18px}
  main{padding:10px 12px}
}
</style>
</head>
<body>
<header>
  <button class="back" onclick="window.location='/'">← Dashboard</button>
  <div class="logo">Performance &amp; Notifications</div>
</header>
<main>
  <!-- Summary cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value neu" id="s-winrate">—</div>
      <div class="card-sub" id="s-winrate-sub">—</div>
    </div>
    <div class="card">
      <div class="card-label">Avg Gain</div>
      <div class="card-value pos" id="s-avg-gain">—</div>
    </div>
    <div class="card">
      <div class="card-label">Avg Loss</div>
      <div class="card-value neg" id="s-avg-loss">—</div>
    </div>
    <div class="card">
      <div class="card-label">Best Trade</div>
      <div class="card-value pos" id="s-best">—</div>
      <div class="card-sub" id="s-best-sym">—</div>
    </div>
    <div class="card">
      <div class="card-label">Worst Trade</div>
      <div class="card-value neg" id="s-worst">—</div>
      <div class="card-sub" id="s-worst-sym">—</div>
    </div>
    <div class="card">
      <div class="card-label">Total Realized P&amp;L</div>
      <div class="card-value" id="s-total-pnl">—</div>
    </div>
  </div>

  <!-- Risk metric cards -->
  <div class="section-label">Risk Metrics</div>
  <div class="cards">
    <div class="card">
      <div class="card-label">Sharpe Ratio</div>
      <div class="card-value neu" id="s-sharpe">—</div>
      <div id="s-sharpe-rating" class="card-rating rating-na" style="margin-top:4px">Insufficient data</div>
      <div class="card-sub" style="margin-top:4px">Annualised · RF 5%</div>
    </div>
    <div class="card">
      <div class="card-label">Max Drawdown</div>
      <div class="card-value" id="s-maxdd">—</div>
      <div id="s-maxdd-rating" class="card-rating rating-na" style="margin-top:4px">Insufficient data</div>
      <div class="card-sub" id="s-maxdd-dollar" style="margin-top:4px">—</div>
    </div>
    <div class="card">
      <div class="card-label">Current Drawdown</div>
      <div class="card-value" id="s-curdd">—</div>
      <div class="card-sub" style="margin-top:4px">From all-time high</div>
    </div>
    <div class="card">
      <div class="card-label">Calmar Ratio</div>
      <div class="card-value neu" id="s-calmar">—</div>
      <div id="s-calmar-rating" class="card-rating rating-na" style="margin-top:4px">Insufficient data</div>
      <div class="card-sub" style="margin-top:4px">Ann. return ÷ max DD</div>
    </div>
  </div>

  <!-- Drawdown history chart -->
  <div class="panel">
    <div class="panel-title">Drawdown History
      <span style="font-size:11px;color:#475569;margin-left:8px;font-weight:400">% below running peak · shaded area = underwater period</span>
    </div>
    <div style="padding:8px 4px 4px">
      <div id="dd-chart" style="height:200px"></div>
    </div>
  </div>

  <!-- Equity chart -->
  <div class="panel">
    <div class="panel-title">Portfolio Value Over Time</div>
    <div class="chart-wrap" id="chart-wrap">
      <div class="chart-empty">Loading chart…</div>
    </div>
  </div>

  <!-- Period P&L bar chart -->
  <div class="panel">
    <div class="panel-title">P&amp;L by Period
      <span style="margin-left:auto;display:flex;gap:6px" id="period-tabs">
        <button class="tab-btn active" onclick="switchPeriod('daily',this)">Daily</button>
        <button class="tab-btn" onclick="switchPeriod('weekly',this)">Weekly</button>
        <button class="tab-btn" onclick="switchPeriod('monthly',this)">Monthly</button>
      </span>
    </div>
    <div style="padding:8px 4px 4px">
      <div id="period-chart" style="height:240px"></div>
    </div>
  </div>

  <!-- Backtest report -->
  <div class="section-label">Backtest vs S&amp;P 500</div>
  <div class="panel" id="bt-panel">
    <div class="panel-title">6-Month Backtest Report
      <span id="bt-gen-at" style="font-size:11px;color:#475569;font-weight:400;margin-left:8px">—</span>
      <span id="bt-running-badge" style="display:none;margin-left:8px;font-size:10px;padding:2px 8px;border-radius:99px;background:#1e3a5f;color:#93c5fd;font-weight:600">Running…</span>
    </div>
    <div id="bt-body">
      <div style="padding:22px;text-align:center;color:#475569;font-size:13px">
        No report yet — run a backtest to compare algo performance vs S&amp;P 500.<br>
        <em style="font-size:11px">Auto-runs every Friday after market close.</em>
      </div>
    </div>
    <div class="bt-run-wrap">
      <button id="btn-bt-run" onclick="runBacktest()" style="background:#7c3aed;color:#fff;padding:7px 18px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer">▶ Run Now</button>
      <span style="font-size:12px;color:#475569">Backtests the current watchlist over the past 6 months — runs in background.</span>
    </div>
  </div>

  <!-- Notifications setup -->
  <div class="panel" id="notif-panel">
    <div class="panel-title">🔔 Trade Notifications</div>
    <div id="notif-body"></div>
  </div>

  <!-- All trades -->
  <div class="panel">
    <div class="panel-title">All Trades <span id="trade-ct" style="background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px">0</span></div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>Realized P&amp;L</th><th>Reason</th>
        </tr></thead>
        <tbody id="trade-body"><tr><td colspan="7" class="empty">No trades yet</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Trade journal — from persisted JSONL with indicator snapshots -->
  <div class="panel">
    <div class="panel-title">
      Trade Journal
      <span id="journal-ct" style="background:#334155;color:#94a3b8;border-radius:99px;padding:1px 8px;font-size:11px">0</span>
      <span style="color:#475569;font-size:11px;margin-left:8px">persisted · includes indicator snapshots</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>Time</th><th>Ticker</th><th>Side</th><th>Qty</th><th>Price</th><th>P&amp;L</th><th>RSI</th><th>Score</th><th>Reason</th>
        </tr></thead>
        <tbody id="journal-body"><tr><td colspan="9" class="empty">Loading…</td></tr></tbody>
      </table>
    </div>
  </div>
</main>

<script>
const fmt = (n, dec=2) => n == null ? '—' : n.toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec});
const fmtD = (n, prefix='$') => n == null ? '—' : (n>=0?'+':'-') + prefix + fmt(Math.abs(n));
const cls  = n => n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu';

function renderCards(data) {
  const ts = data.trade_stats;
  if (!ts || ts.sell_trades === 0) {
    ['s-winrate','s-avg-gain','s-avg-loss','s-best','s-worst','s-total-pnl']
      .forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '—'; });
    document.getElementById('s-winrate-sub').textContent = 'No closed trades yet';
    return;
  }
  const winEl = document.getElementById('s-winrate');
  winEl.textContent = ts.win_rate + '%';
  winEl.className = 'card-value ' + (ts.win_rate >= 50 ? 'pos' : 'neg');
  document.getElementById('s-winrate-sub').textContent = ts.sell_trades + ' closed trades';

  document.getElementById('s-avg-gain').textContent  = ts.avg_gain  ? '+$' + fmt(ts.avg_gain)  : '—';
  document.getElementById('s-avg-loss').textContent  = ts.avg_loss  ? '-$' + fmt(Math.abs(ts.avg_loss))  : '—';
  document.getElementById('s-best').textContent      = ts.best_trade  != null ? fmtD(ts.best_trade)  : '—';
  document.getElementById('s-worst').textContent     = ts.worst_trade != null ? fmtD(ts.worst_trade) : '—';

  const tpEl = document.getElementById('s-total-pnl');
  tpEl.textContent  = fmtD(ts.total_realized_pnl);
  tpEl.className    = 'card-value ' + cls(ts.total_realized_pnl);
}

function renderChart(snapshots, initialCapital) {
  const wrap = document.getElementById('chart-wrap');
  if (!snapshots || snapshots.length < 2) {
    wrap.innerHTML = '<div class="chart-empty">Not enough data yet — chart updates as cycles run</div>';
    return;
  }

  const W = 900, H = 220, PL = 56, PR = 16, PT = 16, PB = 36;
  const iW = W - PL - PR, iH = H - PT - PB;

  const values = snapshots.map(s => s.value);
  const allVals = [...values, initialCapital];
  const minV = Math.min(...allVals);
  const maxV = Math.max(...allVals);
  const range = maxV - minV || 1;

  const px = i => PL + (i / (snapshots.length - 1)) * iW;
  const py = v => PT + (1 - (v - minV) / range) * iH;

  // Polyline points
  const pts = snapshots.map((s,i) => `${px(i).toFixed(1)},${py(s.value).toFixed(1)}`).join(' ');

  // Fill polygon (line + baseline)
  const lastX = px(snapshots.length - 1);
  const baselineY = py(Math.max(minV, Math.min(maxV, initialCapital)));
  const fillPts = `${PL},${baselineY} ${pts} ${lastX},${baselineY}`;

  const lastVal  = values[values.length - 1];
  const lineCol  = lastVal >= initialCapital ? '#22c55e' : '#ef4444';
  const fillCol  = lastVal >= initialCapital ? 'rgba(34,197,94,.1)' : 'rgba(239,68,68,.1)';
  const baselineY2 = py(initialCapital);

  // Y-axis labels (3 ticks)
  const ticks = [minV, (minV+maxV)/2, maxV];
  const yLabels = ticks.map(v =>
    `<text x="${PL-6}" y="${(py(v)+4).toFixed(1)}" text-anchor="end" fill="#475569" font-size="10">\$${(v/1000).toFixed(1)}k</text>`
  ).join('');

  // X-axis labels (first + last)
  const fmtTs = iso => { const d=new Date(iso); return (d.getMonth()+1)+'/'+d.getDate()+' '+d.getHours()+':'+(d.getMinutes()+'').padStart(2,'0'); };
  const xFirst = `<text x="${PL}" y="${H-8}" text-anchor="start" fill="#475569" font-size="10">${fmtTs(snapshots[0].ts)}</text>`;
  const xLast  = `<text x="${lastX}" y="${H-8}" text-anchor="end" fill="#475569" font-size="10">${fmtTs(snapshots[snapshots.length-1].ts)}</text>`;

  wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block">
    <polygon points="${fillPts}" fill="${fillCol}"/>
    <line x1="${PL}" y1="${baselineY2.toFixed(1)}" x2="${W-PR}" y2="${baselineY2.toFixed(1)}"
          stroke="#334155" stroke-width="1" stroke-dasharray="5,4"/>
    <polyline points="${pts}" fill="none" stroke="${lineCol}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${px(snapshots.length-1).toFixed(1)}" cy="${py(lastVal).toFixed(1)}" r="3.5" fill="${lineCol}"/>
    ${yLabels}${xFirst}${xLast}
    <text x="${W/2}" y="${H-8}" text-anchor="middle" fill="#334155" font-size="10">— initial capital</text>
  </svg>`;
}

function renderNotifications(n) {
  const body = document.getElementById('notif-body');
  const ntfyOn = n && n.ntfy_enabled;
  const poOn   = n && n.pushover_enabled;

  body.innerHTML = `
    <div class="notif-row">
      <div class="notif-icon">📲</div>
      <div>
        <div class="notif-head">ntfy.sh (free, no account needed) <span class="${ntfyOn?'badge-on':'badge-off'}">${ntfyOn?'ON':'OFF'}</span></div>
        <div class="notif-body">
          ${ntfyOn
            ? `Sending alerts to topic <code>${n.ntfy_topic}</code>. Subscribe at <code>https://ntfy.sh/${n.ntfy_topic}</code> or in the ntfy app.`
            : `Add <code>NTFY_TOPIC=your-topic-name</code> to your <code>.env</code> file, then restart the dashboard.<br>
               Install the <strong>ntfy</strong> app on your phone and subscribe to the same topic — no account needed.`}
        </div>
      </div>
    </div>
    <div class="notif-row">
      <div class="notif-icon">🔔</div>
      <div>
        <div class="notif-head">Pushover <span class="${poOn?'badge-on':'badge-off'}">${poOn?'ON':'OFF'}</span></div>
        <div class="notif-body">
          ${poOn
            ? 'Pushover notifications are active.'
            : `Add <code>PUSHOVER_TOKEN=your-app-token</code> and <code>PUSHOVER_USER=your-user-key</code> to your <code>.env</code> file.<br>
               Get credentials at <strong>pushover.net</strong> (one-time $5 purchase, iOS &amp; Android).`}
        </div>
      </div>
    </div>
    <div class="notif-row">
      <div class="notif-icon">⚡</div>
      <div>
        <div class="notif-head">What triggers an alert</div>
        <div class="notif-body">
          <strong>BUY executed</strong> — symbol, shares, price, reason<br>
          <strong>SELL executed</strong> — symbol, realized P&amp;L (high priority if loss)<br>
          <strong>Stop-loss triggered</strong> — same as sell, marked as stop<br>
          <strong>VOO 200W MA alert</strong> — fires once per day when VOO crosses or is near the MA
        </div>
      </div>
    </div>`;
}

function renderTrades(trades) {
  document.getElementById('trade-ct').textContent = trades.length;
  const tbody = document.getElementById('trade-body');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">No trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlStr = t.pnl != null
      ? `<span class="${t.pnl>0?'pos':'neg'}">${t.pnl>=0?'+':'-'}$${fmt(Math.abs(t.pnl))} (${t.pnl_pct>=0?'+':''}${fmt(t.pnl_pct)}%)</span>`
      : '—';
    return `<tr>
      <td style="color:#64748b;font-size:11px">${t.timestamp}</td>
      <td style="font-weight:600">${t.symbol}</td>
      <td><span class="pill pill-${t.action}">${t.action}</span></td>
      <td>${t.shares}</td>
      <td>$${fmt(t.price)}</td>
      <td>${pnlStr}</td>
      <td style="color:#64748b;font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${t.reason||'—'}</td>
    </tr>`;
  }).join('');
}

function renderJournal(entries) {
  const ct = document.getElementById('journal-ct');
  const tbody = document.getElementById('journal-body');
  ct.textContent = entries.length;
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">No journal entries yet — trades are logged automatically</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(e => {
    const ind = e.indicators || {};
    const rsi   = ind.rsi    != null ? fmt(ind.rsi, 1)    : '—';
    const score = ind.score  != null ? ((ind.score >= 0 ? '+' : '') + fmt(ind.score, 3)) : '—';
    const scoreCol = ind.score > 0 ? '#4ade80' : ind.score < 0 ? '#f87171' : '#94a3b8';
    const pnlStr = e.pnl != null
      ? `<span class="${e.pnl>0?'pos':'neg'}">${e.pnl>=0?'+':'-'}$${fmt(Math.abs(e.pnl))}${e.pnl_pct!=null?' ('+((e.pnl_pct>=0?'+':'')+fmt(e.pnl_pct*100))+'%)':''}</span>`
      : '—';
    const ts = e.timestamp ? e.timestamp.replace('T',' ').slice(0,16) : '—';
    return `<tr>
      <td style="color:#64748b;font-size:11px">${ts}</td>
      <td style="font-weight:600">${e.symbol}</td>
      <td><span class="pill pill-${e.action}">${e.action}</span></td>
      <td>${e.shares}</td>
      <td>$${fmt(e.price)}</td>
      <td>${pnlStr}</td>
      <td style="color:#94a3b8">${rsi}</td>
      <td style="color:${scoreCol};font-weight:600">${score}</td>
      <td style="color:#64748b;font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${e.reason||'—'}</td>
    </tr>`;
  }).join('');
}

// ── Risk metrics ─────────────────────────────────────────────────────────────
function sharpeRating(v) {
  if (v == null) return ['na',  'Insufficient data'];
  if (v >= 2.0)  return ['great','Excellent (>2)'];
  if (v >= 1.0)  return ['good', 'Good (1–2)'];
  if (v >= 0.5)  return ['ok',   'Mediocre (0.5–1)'];
  if (v >= 0.0)  return ['bad',  'Poor (0–0.5)'];
  return          ['bad',  'Negative — underperforming risk-free'];
}
function ddRating(pct) {
  if (pct == null) return ['na', 'Insufficient data'];
  if (pct < 5)     return ['great', 'Excellent (<5%)'];
  if (pct < 10)    return ['good',  'Good (5–10%)'];
  if (pct < 20)    return ['ok',    'Moderate (10–20%)'];
  return            ['bad',  'High risk (>20%)'];
}
function calmarRating(v) {
  if (v == null) return ['na',  'Insufficient data'];
  if (v >= 3.0)  return ['great','Excellent (>3)'];
  if (v >= 1.0)  return ['good', 'Good (1–3)'];
  if (v >= 0.5)  return ['ok',   'Mediocre (0.5–1)'];
  return          ['bad',  'Poor (<0.5)'];
}

function renderRiskMetrics(rm) {
  if (!rm) return;
  const fmt2 = n => n == null ? '—' : n.toFixed(2);

  // Sharpe
  const sharpeEl = document.getElementById('s-sharpe');
  const sharpeR  = document.getElementById('s-sharpe-rating');
  sharpeEl.textContent = fmt2(rm.sharpe);
  const [sc, sl] = sharpeRating(rm.sharpe);
  sharpeEl.className = 'card-value ' + (rm.sharpe == null ? 'neu' : rm.sharpe >= 1 ? 'pos' : rm.sharpe < 0 ? 'neg' : 'neu');
  sharpeR.className  = `card-rating rating-${sc}`;
  sharpeR.textContent = sl;

  // Max drawdown
  const maxddEl = document.getElementById('s-maxdd');
  const maxddR  = document.getElementById('s-maxdd-rating');
  const maxddSub = document.getElementById('s-maxdd-dollar');
  maxddEl.textContent = rm.max_drawdown_pct != null ? '-' + fmt2(rm.max_drawdown_pct) + '%' : '—';
  maxddEl.className = 'card-value ' + (rm.max_drawdown_pct > 0 ? 'neg' : 'neu');
  const [dc, dl] = ddRating(rm.max_drawdown_pct);
  maxddR.className  = `card-rating rating-${dc}`;
  maxddR.textContent = dl;
  maxddSub.textContent = rm.max_drawdown_dollar > 0 ? '-$' + rm.max_drawdown_dollar.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}) : '—';

  // Current drawdown
  const curddEl = document.getElementById('s-curdd');
  const pct = rm.current_drawdown_pct;
  curddEl.textContent = pct != null ? (pct > 0 ? '-' + fmt2(pct) + '%' : '0.00% — At peak') : '—';
  curddEl.className = 'card-value ' + (pct > 0 ? 'neg' : 'pos');

  // Calmar
  const calmarEl = document.getElementById('s-calmar');
  const calmarR  = document.getElementById('s-calmar-rating');
  calmarEl.textContent = fmt2(rm.calmar);
  calmarEl.className = 'card-value ' + (rm.calmar == null ? 'neu' : rm.calmar >= 1 ? 'pos' : rm.calmar < 0 ? 'neg' : 'neu');
  const [cc, cl] = calmarRating(rm.calmar);
  calmarR.className  = `card-rating rating-${cc}`;
  calmarR.textContent = cl;
}

function renderDrawdownChart(curve) {
  const el = document.getElementById('dd-chart');
  if (!el) return;
  if (!curve || curve.length < 2) {
    el.innerHTML = '<div style="color:#475569;text-align:center;padding:60px 0;font-size:13px">Not enough data — chart updates as cycles run</div>';
    return;
  }
  const xs = curve.map(p => p.ts);
  const ys = curve.map(p => p.dd);   // already negative (%  below peak)
  const minY = Math.min(...ys, -0.1);

  Plotly.react(el, [
    // Shaded fill under the curve (underwater area)
    {type: 'scatter', x: xs, y: ys, fill: 'tozeroy',
     fillcolor: 'rgba(239,68,68,0.15)', line: {color: '#ef4444', width: 1.5},
     name: 'Drawdown', hovertemplate: '%{x}<br>%{y:.2f}%<extra></extra>'},
  ], {
    paper_bgcolor: '#0f172a', plot_bgcolor: '#0f172a',
    font: {color: '#94a3b8', family: 'Segoe UI,system-ui,sans-serif', size: 11},
    margin: {l: 52, r: 16, t: 8, b: 42},
    xaxis: {type: 'date', tickfont: {size: 9, color: '#475569'}, gridcolor: '#1e293b',
            rangeslider: {visible: false}},
    yaxis: {ticksuffix: '%', tickfont: {size: 10, color: '#475569'}, gridcolor: '#1e293b',
            zeroline: true, zerolinecolor: '#334155', range: [minY * 1.1, 0.5]},
    showlegend: false,
  }, {responsive: true, displayModeBar: false});
}

// ── Period P&L bar chart ──────────────────────────────────────────────────────
let _periodData = null;
let _activePeriod = 'daily';

function renderPeriodChart(periodKey) {
  const el = document.getElementById('period-chart');
  if (!el || !_periodData) return;
  const rows = (_periodData[periodKey] || []);
  if (!rows.length) {
    el.innerHTML = '<div style="color:#475569;text-align:center;padding:60px 0;font-size:13px">No closed trades yet</div>';
    return;
  }
  // Filter trailing zero-only rows from daily view to keep chart tight
  let display = rows;
  if (periodKey === 'daily') {
    const lastNonZero = rows.reduce((idx, r, i) => r.pnl !== 0 ? i : idx, -1);
    display = lastNonZero >= 0 ? rows.slice(Math.max(0, lastNonZero - 13), lastNonZero + 1) : rows.slice(-14);
  }
  const labels = display.map(r => r.period);
  const values = display.map(r => r.pnl);
  const colors = values.map(v => v >= 0 ? '#22c55e' : '#ef4444');
  const maxAbs  = Math.max(...values.map(Math.abs), 1);

  Plotly.react(el, [{
    type: 'bar', x: labels, y: values,
    marker: {color: colors, opacity: 0.85},
    hovertemplate: '<b>%{x}</b><br>$%{y:+,.2f}<extra></extra>',
  }], {
    paper_bgcolor: '#0f172a', plot_bgcolor: '#0f172a',
    font: {color: '#94a3b8', family: 'Segoe UI,system-ui,sans-serif', size: 11},
    margin: {l: 62, r: 16, t: 10, b: 56},
    xaxis: {tickfont: {size: 9, color: '#475569'}, gridcolor: '#1e293b',
            tickangle: labels.length > 10 ? -45 : 0},
    yaxis: {tickprefix: '$', tickfont: {size: 10, color: '#475569'}, gridcolor: '#1e293b',
            zeroline: true, zerolinecolor: '#334155', zerolinewidth: 1,
            range: [-maxAbs * 1.15, maxAbs * 1.15]},
    bargap: 0.3, showlegend: false,
  }, {responsive: true, displayModeBar: false});
}

function switchPeriod(key, btn) {
  _activePeriod = key;
  document.querySelectorAll('#period-tabs .tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderPeriodChart(key);
}

fetch('/api/stats')
  .then(r => r.json())
  .then(data => {
    renderCards(data);
    renderChart(data.equity_snapshots, data.initial_capital);
    renderNotifications(data.notifications);
    renderTrades(data.trades);
    _periodData = data.period_pnl || null;
    renderPeriodChart(_activePeriod);
    renderRiskMetrics(data.risk_metrics || null);
    renderDrawdownChart((data.risk_metrics || {}).drawdown_curve || []);
  })
  .catch(e => {
    document.querySelector('main').innerHTML = '<p style="color:#f87171;padding:24px">Failed to load stats: ' + e + '</p>';
  });

fetch('/api/journal')
  .then(r => r.json())
  .then(data => { if (data.ok) renderJournal(data.entries); })
  .catch(() => {
    document.getElementById('journal-body').innerHTML =
      '<tr><td colspan="9" class="empty" style="color:#f87171">Failed to load journal</td></tr>';
  });

// ── Backtest report ───────────────────────────────────────────────────────────
function renderBacktest(report) {
  const body   = document.getElementById('bt-body');
  const genAt  = document.getElementById('bt-gen-at');
  if (!report || report.error) {
    body.innerHTML = `<div style="padding:18px 16px;color:#f87171;font-size:13px">
      ${report ? 'Error: ' + report.error : 'No report yet.'}</div>`;
    return;
  }
  const a   = report.algo || {};
  const spy = report.spy_return_pct;
  const gen = report.generated_at ? new Date(report.generated_at).toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'}) : '—';
  genAt.textContent = 'Generated ' + gen + '  ·  ' + (report.period_start||'') + ' → ' + (report.period_end||'');

  const clsR = v => v == null ? 'neu' : v > 0 ? 'pos' : v < 0 ? 'neg' : 'neu';
  const fmtR = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%';

  const beatsHtml = report.beats_spy != null
    ? `<div class="bt-beats ${report.beats_spy ? 'bt-beats-yes' : 'bt-beats-no'}">
         ${report.beats_spy
           ? '✓ Algo outperformed SPY (+' + (a.total_return_pct - spy).toFixed(2) + '% alpha)'
           : '✗ Algo underperformed SPY by ' + (spy - a.total_return_pct).toFixed(2) + '%'}
       </div>`
    : '';

  body.innerHTML = beatsHtml + `
    <div class="bt-grid">
      <div class="bt-card">
        <div class="bt-label">Algo 6M Return</div>
        <div class="bt-value ${clsR(a.total_return_pct)}">${fmtR(a.total_return_pct)}</div>
        <div class="bt-sub">Annualised: ${fmtR(a.annualized_return_pct)}</div>
      </div>
      <div class="bt-card">
        <div class="bt-label">SPY 6M Return</div>
        <div class="bt-value ${clsR(spy)}">${fmtR(spy)}</div>
        <div class="bt-sub">Buy &amp; hold benchmark</div>
      </div>
      <div class="bt-card">
        <div class="bt-label">Sharpe Ratio</div>
        <div class="bt-value neu">${a.sharpe_ratio != null ? a.sharpe_ratio.toFixed(2) : '—'}</div>
        <div class="bt-sub">Risk-adjusted return</div>
      </div>
      <div class="bt-card">
        <div class="bt-label">Max Drawdown</div>
        <div class="bt-value ${a.max_drawdown_pct > 0 ? 'neg' : 'neu'}">${a.max_drawdown_pct != null ? '-' + a.max_drawdown_pct.toFixed(2) + '%' : '—'}</div>
      </div>
      <div class="bt-card">
        <div class="bt-label">Win Rate</div>
        <div class="bt-value ${(a.win_rate_pct||0) >= 50 ? 'pos' : 'neg'}">${a.win_rate_pct != null ? a.win_rate_pct.toFixed(1) + '%' : '—'}</div>
        <div class="bt-sub">${a.total_trades||0} trades</div>
      </div>
      <div class="bt-card">
        <div class="bt-label">Profit Factor</div>
        <div class="bt-value ${(a.profit_factor||0) >= 1 ? 'pos' : 'neg'}">${a.profit_factor != null ? a.profit_factor.toFixed(2) : '—'}</div>
        <div class="bt-sub">Gross wins / gross losses</div>
      </div>
    </div>`;
}

let _btPolling = null;
async function loadBacktest() {
  try {
    const res  = await fetch('/api/backtest/report');
    const data = await res.json();
    const runBadge = document.getElementById('bt-running-badge');
    if (data.running) {
      if (runBadge) runBadge.style.display = 'inline-block';
    } else {
      if (runBadge) runBadge.style.display = 'none';
      if (_btPolling) { clearInterval(_btPolling); _btPolling = null; }
    }
    if (data.ok && data.report) renderBacktest(data.report);
  } catch(e) {}
}

async function runBacktest() {
  const btn = document.getElementById('btn-bt-run');
  const badge = document.getElementById('bt-running-badge');
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    const res  = await fetch('/api/backtest/run', {method:'POST'});
    const data = await res.json();
    if (data.ok) {
      if (badge) badge.style.display = 'inline-block';
      document.getElementById('bt-body').innerHTML =
        '<div style="padding:18px 16px;color:#94a3b8;font-size:13px">Backtest running — this may take a minute…</div>';
      // Poll every 5s until done
      _btPolling = setInterval(loadBacktest, 5000);
    } else {
      alert('Could not start backtest: ' + (data.reason || 'unknown error'));
    }
  } catch(e) {
    alert('Backtest request failed: ' + e);
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Now';
  }
}

loadBacktest();

// ── Service Worker (PWA) ──────────────────────────────────────────────────────
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>"""


@app.route("/leaderboard")
def leaderboard_page():
    resp = make_response(render_template_string(LEADERBOARD_HTML))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/stats")
def stats_page():
    return render_template_string(STATS_HTML)


@app.route("/journal")
def journal_page():
    if _AUTH_ENABLED and not session.get("logged_in"):
        return redirect("/login?next=/journal")
    resp = make_response(render_template_string(JOURNAL_HTML))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/settings")
def settings_page():
    if _AUTH_ENABLED and not session.get("logged_in"):
        return redirect("/login?next=/settings")
    profiles_json = json.dumps({
        k: {"label": v["label"], "tagline": v["tagline"], "color": v["color"],
            "score_label": v.get("score_label", ""), "overrides": v["overrides"]}
        for k, v in RISK_PROFILES.items()
    })
    eng = _get_engine()
    alpaca_connected = False
    alpaca_paper     = True
    if _AUTH_ENABLED:
        user_id = session.get("user_id")
        if user_id:
            _u = db.session.get(User, user_id)
            if _u and _u.alpaca_api_key_enc:
                alpaca_connected = True
                alpaca_paper     = bool(_u.alpaca_paper)
    resp = make_response(render_template_string(
        SETTINGS_HTML,
        profiles_json=profiles_json,
        current_profile=_current_profile,
        email_configured=eng.emailer.is_configured,
        email_active=eng.emailer.active,
        notify_email=eng.emailer.notify_email,
        alpaca_connected=alpaca_connected,
        alpaca_paper=alpaca_paper,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/")
def home():
    resp = make_response(render_template_string(
        _LANDING_HTML,
        auth=_AUTH_ENABLED,
        logged_in=bool(session.get("logged_in")) if _AUTH_ENABLED else True,
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/dashboard")
def index():
    # Belt-and-suspenders: guard even if before_request is bypassed
    if _AUTH_ENABLED and not session.get("logged_in"):
        logging.warning("[AUTH] /dashboard hit without session — redirecting to login")
        return redirect("/login?next=/dashboard")
    alpaca_connected = False
    if _AUTH_ENABLED:
        user_id = session.get("user_id")
        if user_id:
            try:
                _u = db.session.get(User, user_id)
                if _u and _u.alpaca_api_key_enc:
                    alpaca_connected = True
            except Exception:
                pass
    resp = make_response(render_template_string(HTML, auth=_AUTH_ENABLED, alpaca_connected=alpaca_connected))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="NYSE Trading Engine Dashboard")
    parser.add_argument(
        "--tunnel", action="store_true",
        help="Open a public ngrok tunnel so you can access the dashboard from anywhere",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="Local port to serve on (default: 8080)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    t = threading.Thread(target=_background_loop, daemon=True, name="cycle-scheduler")
    t.start()
    log.info(f"Cycle scheduler started — running every {CYCLE_INTERVAL}s")

    local_url = f"http://localhost:{args.port}"
    print(f"\n  Local:   {local_url}")

    # Auto-start ngrok if --tunnel passed OR NGROK_AUTHTOKEN env var is set
    ngrok_auth = os.getenv("NGROK_AUTHTOKEN") or os.getenv("NGROK_TOKEN")
    if args.tunnel or ngrok_auth:
        try:
            from pyngrok import conf, ngrok
            if ngrok_auth:
                conf.get_default().auth_token = ngrok_auth
            tunnel = ngrok.connect(args.port)
            _public_url = tunnel.public_url.replace("http://", "https://")
            print(f"  Public:  {_public_url}")
            print(f"  ← access from phone on cellular, work network, anywhere\n")
        except ImportError:
            print("\n  ERROR: pyngrok not installed — run:  pip install pyngrok\n")
            if args.tunnel:
                sys.exit(1)
        except Exception as e:
            print(f"\n  WARNING: ngrok tunnel failed: {e}")
            print("  Continuing without tunnel — local access only.\n")

    print(f"  Auto-cycle every {CYCLE_INTERVAL}s\n")
    app.run(host="0.0.0.0", port=args.port, debug=False, use_reloader=False)
