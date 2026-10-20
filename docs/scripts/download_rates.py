# Read-only download-rate report from track_history (source = 'downloaded').
# Run on the host: docker exec -i spotiflac-web python - < download_rates.py
import sqlite3, datetime as dt, collections
DAYS, GAP_MIN = 4, 20  # look-back; a gap longer than this starts a new session
db = sqlite3.connect('file:/app/data/spotiflac.db?mode=ro', uri=True)
since = (dt.datetime.utcnow() - dt.timedelta(days=DAYS)).isoformat(' ')
ts = [dt.datetime.fromisoformat(r[0][:19]) for r in db.execute(
    "select first_seen_at from track_history where source='downloaded' and first_seen_at >= ? "
    "order by first_seen_at", (since,))]
print(f"{len(ts)} downloaded tracks since {since[:16]} UTC")
print("other sources:", db.execute("select source, count(*) from track_history group by source").fetchall())
if not ts: raise SystemExit

print(f"\n== Sessions (gap > {GAP_MIN} min splits) ==")
sessions, cur = [], [ts[0]]
for a, b in zip(ts, ts[1:]):
    if (b - a).total_seconds() > GAP_MIN * 60: sessions.append(cur); cur = []
    cur.append(b)
sessions.append(cur)
for s in sessions:
    mins = max(1, (s[-1] - s[0]).total_seconds() / 60)
    print(f"{s[0]:%m-%d %H:%M} -> {s[-1]:%H:%M}  {mins:6.0f} min  {len(s):4d} tracks  {len(s)/mins*60:5.1f}/h")

def buckets(minutes):
    c = collections.Counter(t.replace(minute=t.minute - t.minute % minutes if minutes < 60 else 0, second=0) for t in ts)
    return sorted(c.items())

print("\n== Per hour ==")
for k, n in buckets(60): print(f"{k:%m-%d %H}:00  {n:4d}  {'#' * min(n, 80)}")
ten = buckets(10)
print("\n== Per 10 min (non-empty) ==")
for k, n in ten: print(f"{k:%m-%d %H:%M}  {n:3d}  {'#' * n}")
vals = sorted(n for _, n in ten)
print(f"\n10-min buckets: max {vals[-1]}, median {vals[len(vals)//2]}, mean {sum(vals)/len(vals):.1f}")

