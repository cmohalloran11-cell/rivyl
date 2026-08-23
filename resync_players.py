"""One-off maintenance script: force a full player-pool resync.

sync_players() only populates the table when it's empty, so an already-synced
production database won't pick up new source data (like the fallback pool of
non-top-500 players) on its own. Run this once after deploying that change.

Requires DATABASE_URL (or POSTGRES_URL) to be set, same as the app itself.

    python resync_players.py
"""
import app

with app.app.app_context():
    db = app.get_db()
    before = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    ok = app.sync_players(db, force=True)
    after = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    db.close()

if ok:
    print(f"Resynced players: {before} -> {after}")
else:
    print("Resync failed (no network reaching the rankings/Sleeper data?).")
