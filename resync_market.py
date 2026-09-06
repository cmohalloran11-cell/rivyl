"""One-off maintenance script: force an immediate refresh of market_projections.

sync_market_projections() only refreshes when its 15-minute cache is stale, so
after a fix to the fetch/mapping logic, the DB can keep serving pre-fix numbers
until that window naturally expires. Run this once after such a deploy to pick
up the fix immediately instead of waiting it out.

Requires DATABASE_URL (or POSTGRES_URL) to be set, same as the app itself.

    python resync_market.py
"""
import app

with app.app.app_context():
    db = app.get_db()
    ok = app.sync_market_projections(db, force=True)
    count = db.execute("SELECT COUNT(*) FROM market_projections").fetchone()[0]
    db.close()

if ok:
    print(f"Resynced market_projections: {count} players now have a market-derived projection.")
else:
    print("Resync failed (no network reaching PrizePicks/Underdog, or no lines matched any player?).")
