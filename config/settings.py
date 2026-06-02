"""Application settings — loaded from environment variables via python-dotenv."""
from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # API Keys
    ODDS_API_KEY: str = os.getenv("ODDS_API_KEY", "")
    BETFAIR_USERNAME: str = os.getenv("BETFAIR_USERNAME", "")
    BETFAIR_PASSWORD: str = os.getenv("BETFAIR_PASSWORD", "")
    BETFAIR_APP_KEY: str = os.getenv("BETFAIR_APP_KEY", "")
    BETFAIR_CERTS_PATH: str | None = os.getenv("BETFAIR_CERTS_PATH")
    SPORTSDATA_API_KEY: str = os.getenv("SPORTSDATA_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    NEWS_API_KEY: str = os.getenv("NEWS_API_KEY", "")
    # GitHub Gist (live dashboard data — no repo commits)
    GH_GIST_TOKEN: str = os.getenv("GH_GIST_TOKEN", "")
    GH_GIST_ID: str = os.getenv("GH_GIST_ID", "647660296d9b6f59377b879d655907e6")

    POLYMARKET_API_KEY: str = os.getenv("POLYMARKET_API_KEY", "")  # optional; not required for read-only market data
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")  # Ethereum private key for live order placement (Polygon)
    POLYMARKET_CHAIN_ID: int = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))  # 137=Polygon mainnet, 80002=Amoy testnet

    # Kalshi prediction markets
    KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
    KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    # Comma-separated category filter; empty = fetch all categories
    # Available: politics, economics, crypto, sports, weather, entertainment, technology, health
    KALSHI_CATEGORIES: list[str] = [
        c for c in os.getenv("KALSHI_CATEGORIES", "").split(",") if c
    ]

    # Database — set DATABASE_URL to your Neon Postgres connection string
    # (postgresql://...). Falls back to a local SQLite DB when unset, so the
    # app still runs in development without any configuration.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///betting_app.db")

    # Claude model
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

    # TheOddsAPI defaults
    ODDS_API_REGIONS: str = os.getenv("ODDS_API_REGIONS", "us,uk,eu,au")
    # h2h is supported on the free tier for all sports; spreads/totals require higher plans
    ODDS_API_MARKETS: str = os.getenv("ODDS_API_MARKETS", "h2h")
    # Sport keys to fetch odds for. Keeping this list short saves API quota.
    # Run `python -m src.cli list-sports` to see all available keys from TheOddsAPI.
    # Defaults cover sports active in early 2026 (NBA, NHL, soccer, MMA, tennis).
    ODDS_API_SPORT_KEYS: list[str] = os.getenv(
        "ODDS_API_SPORT_KEYS",
        "basketball_nba,icehockey_nhl,soccer_epl,soccer_usa_mls,mma_mixed_martial_arts,tennis_atp_french_open",
    ).split(",")

    # Engine defaults
    MIN_EV_THRESHOLD: float = float(os.getenv("MIN_EV_THRESHOLD", "0.03"))
    DEFAULT_SOURCES: list[str] = os.getenv("DEFAULT_SOURCES", "kalshi").split(",")



settings = Settings()
