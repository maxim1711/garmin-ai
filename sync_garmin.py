"""
Garmin Connect -> markdown/JSON sync script.

Usage:
  python sync_garmin.py --login                        # one-time login, prints base64 token
  python sync_garmin.py --days 3 --sink files --out ./garmin
  python sync_garmin.py --days 3 --sink supabase       # needs GARMIN_INGEST_URL + GARMIN_INGEST_SECRET
"""

import argparse
import base64
import json
import os
import pickle
import re
import sys
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Optional dependency guard
# ---------------------------------------------------------------------------
try:
    import garminconnect
except ImportError:
    sys.exit(
        "Missing dependency. Run:  pip install garminconnect\n"
        "Then re-run this script."
    )

TOKEN_FILE = Path(__file__).parent / ".garmin_token"

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _save_token(client: garminconnect.Garmin) -> str:
    raw = pickle.dumps(client)
    b64 = base64.b64encode(raw).decode()
    TOKEN_FILE.write_text(b64)
    return b64


def _load_token() -> garminconnect.Garmin:
    # prefer env var (for GitHub Actions)
    b64 = os.environ.get("GARMIN_TOKEN_B64") or TOKEN_FILE.read_text().strip()
    raw = base64.b64decode(b64)
    return pickle.loads(raw)


def do_login() -> None:
    email = input("Garmin email: ")
    password = input("Garmin password: ")
    client = garminconnect.Garmin(email, password)
    client.login()
    b64 = _save_token(client)
    print("\nLogin successful. Token saved to", TOKEN_FILE)
    print("\nFor GitHub Actions, add this as secret GARMIN_TOKEN_B64:\n")
    print(b64)


def get_client() -> garminconnect.Garmin:
    try:
        client = _load_token()
        client.login()
        return client
    except Exception as exc:
        sys.exit(f"Could not authenticate: {exc}\nRun --login first.")


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_day(client: garminconnect.Garmin, day: date) -> dict:
    ds = day.isoformat()
    data = {"date": ds}

    # Wellness / stats
    try:
        stats = client.get_stats(ds)
        data["resting_hr"]      = stats.get("restingHeartRate")
        data["steps"]           = stats.get("totalSteps")
        data["avg_stress"]      = stats.get("averageStressLevel")
        data["body_battery_lo"] = stats.get("bodyBatteryMostRecentValue")  # end-of-day
        data["body_battery_hi"] = stats.get("bodyBatteryHighestValue")
    except Exception:
        pass

    # Sleep
    try:
        sleep = client.get_sleep_data(ds)
        daily = sleep.get("dailySleepDTO", {})
        data["sleep_duration_h"] = round(
            (daily.get("sleepTimeSeconds") or 0) / 3600, 1
        )
        data["sleep_score"] = daily.get("sleepScores", {}).get("overall", {}).get("value")
        data["sleep_rem_h"] = round((daily.get("remSleepSeconds") or 0) / 3600, 1) or None
        data["sleep_deep_h"] = round((daily.get("deepSleepSeconds") or 0) / 3600, 1) or None
    except Exception:
        pass

    # HRV
    try:
        hrv = client.get_hrv_data(ds)
        summary = hrv.get("hrvSummary", {})
        data["hrv_ms"] = summary.get("lastNight")
        data["hrv_baseline_lo"] = summary.get("baselineLowUpper")
        data["hrv_baseline_hi"] = summary.get("baselineHighUpper")
    except Exception:
        pass

    # Training readiness
    try:
        tr = client.get_training_readiness(ds)
        if tr:
            data["training_readiness"] = tr[0].get("score")
    except Exception:
        pass

    # VO2max and training status (fetched once per day, not per activity)
    try:
        metrics = client.get_max_metrics(ds)
        if metrics:
            m = metrics[0]
            data["vo2max"] = m.get("generic", {}).get("vo2MaxValue")
    except Exception:
        pass

    try:
        ts = client.get_training_status(ds)
        if ts:
            data["training_status"] = ts[0].get("mostRecentTrainingStatus")
            data["training_load_7d"] = ts[0].get("latestEnhancedTrainingLoad7Days")
            data["training_load_balance"] = ts[0].get("mostRecentTrainingLoadBalance")
    except Exception:
        pass

    # Intensity minutes (weekly rolling)
    try:
        im = client.get_intensity_minutes_data(ds)
        if im:
            data["intensity_min_weekly"] = im.get("weeklyIntensityMinutes")
            data["intensity_min_moderate"] = im.get("weeklyModerateIntensityMinutes")
            data["intensity_min_vigorous"] = im.get("weeklyVigorousIntensityMinutes")
    except Exception:
        pass

    # Activities — basic fields first, then enrich with detail call
    try:
        acts = client.get_activities_by_date(ds, ds)
        enriched = []
        for a in acts:
            act = {
                "activity_id":  a.get("activityId"),
                "name":         a.get("activityName"),
                "type":         a.get("activityType", {}).get("typeKey"),
                "start":        a.get("startTimeLocal"),
                "duration_s":   a.get("duration"),
                "distance_m":   a.get("distance"),
                "avg_hr":       a.get("averageHR"),
                "max_hr":       a.get("maxHR"),
                "calories":     a.get("calories"),
                "aerobic_te":   a.get("aerobicTrainingEffect"),
                "anaerobic_te": a.get("anaerobicTrainingEffect"),
                "training_load": a.get("activityTrainingLoad"),
                "avg_pace_s_km": _pace(a.get("averageSpeed"), a.get("activityType", {}).get("typeKey")),
            }
            # Per-activity detail for elevation + laps count
            try:
                detail = client.get_activity(a["activityId"])
                act["elevation_gain_m"] = detail.get("summaryDTO", {}).get("elevationGain")
                act["num_laps"] = detail.get("metaData", {}).get("lapCount")
            except Exception:
                pass
            enriched.append(act)
        data["activities"] = enriched
    except Exception:
        data["activities"] = []

    return data


def _pace(speed_ms: float | None, type_key: str | None) -> int | None:
    """Convert m/s to seconds-per-km for running/walking; return None otherwise."""
    if not speed_ms or speed_ms <= 0:
        return None
    if type_key not in ("running", "trail_running", "treadmill_running", "walking", "hiking"):
        return None
    return round(1000 / speed_ms)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _dur(seconds: float | None) -> str:
    if not seconds:
        return "?"
    m = int(seconds) // 60
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def _dist(meters: float | None) -> str:
    if not meters:
        return "?"
    return f"{meters / 1000:.2f} km"


def render_wellness(data: dict) -> str:
    d = data
    lines = [f"# Garmin wellness {d['date']}"]

    if d.get("resting_hr"):
        lines.append(f"- Resting HR: {d['resting_hr']} bpm")
    if d.get("hrv_ms"):
        baseline = ""
        if d.get("hrv_baseline_lo") and d.get("hrv_baseline_hi"):
            baseline = f" (baseline {d['hrv_baseline_lo']}–{d['hrv_baseline_hi']} ms)"
        lines.append(f"- HRV (overnight): {d['hrv_ms']} ms{baseline}")
    if d.get("sleep_duration_h"):
        score = f" (score {d['sleep_score']})" if d.get("sleep_score") else ""
        lines.append(f"- Sleep: {d['sleep_duration_h']} h{score}")
        if d.get("sleep_deep_h") or d.get("sleep_rem_h"):
            lines.append(f"  - Deep: {d.get('sleep_deep_h') or '?'} h  REM: {d.get('sleep_rem_h') or '?'} h")
    lo = d.get("body_battery_lo")
    hi = d.get("body_battery_hi")
    if lo is not None or hi is not None:
        lines.append(f"- Body battery: {hi or '?'} -> {lo or '?'}")
    if d.get("avg_stress") is not None:
        lines.append(f"- Stress (avg): {d['avg_stress']}")
    if d.get("steps"):
        lines.append(f"- Steps: {d['steps']}")
    if d.get("training_readiness") is not None:
        lines.append(f"- Training readiness: {d['training_readiness']}")
    if d.get("vo2max") is not None:
        lines.append(f"- VO2max: {d['vo2max']}")
    if d.get("training_status"):
        lines.append(f"- Training status: {d['training_status']}")
    if d.get("training_load_7d") is not None:
        balance = f" ({d['training_load_balance']})" if d.get("training_load_balance") else ""
        lines.append(f"- Training load (7d): {round(d['training_load_7d'])}{balance}")
    if d.get("intensity_min_weekly") is not None:
        lines.append(f"- Intensity min (weekly): {d['intensity_min_weekly']} total  "
                     f"({d.get('intensity_min_moderate') or 0} mod + {d.get('intensity_min_vigorous') or 0} vig)")

    return "\n".join(lines) + "\n"


def render_activity(act: dict, day_str: str) -> tuple[str, str]:
    name_slug = re.sub(r"[^a-z0-9]+", "-", (act.get("name") or "workout").lower()).strip("-")
    filename = f"{day_str}-{name_slug}.md"
    lines = [
        f"# {act.get('name') or 'Workout'} — {day_str}",
        f"- Type: {act.get('type', '?')}",
        f"- Start: {act.get('start', '?')}",
        f"- Duration: {_dur(act.get('duration_s'))}",
        f"- Distance: {_dist(act.get('distance_m'))}",
        f"- Avg HR: {act.get('avg_hr') or '?'} bpm",
        f"- Max HR: {act.get('max_hr') or '?'} bpm",
        f"- Calories: {act.get('calories') or '?'} kcal",
    ]
    if act.get("avg_pace_s_km"):
        m, s = divmod(act["avg_pace_s_km"], 60)
        lines.append(f"- Avg pace: {m}:{s:02d} /km")
    if act.get("elevation_gain_m") is not None:
        lines.append(f"- Elevation gain: {round(act['elevation_gain_m'])} m")
    if act.get("aerobic_te") is not None:
        lines.append(f"- Aerobic TE: {act['aerobic_te']:.1f}")
    if act.get("anaerobic_te") is not None:
        lines.append(f"- Anaerobic TE: {act['anaerobic_te']:.1f}")
    if act.get("training_load") is not None:
        lines.append(f"- Training load: {round(act['training_load'])}")
    return filename, "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------

def sink_files(days_data: list[dict], out_dir: Path) -> None:
    daily_dir = out_dir / "daily"
    act_dir   = out_dir / "activities"
    daily_dir.mkdir(parents=True, exist_ok=True)
    act_dir.mkdir(parents=True, exist_ok=True)

    store = {}
    store_path = out_dir / "data.json"
    if store_path.exists():
        store = json.loads(store_path.read_text())

    for d in days_data:
        ds = d["date"]
        store[ds] = d

        (daily_dir / f"{ds}.md").write_text(render_wellness(d), encoding="utf-8")

        for act in d.get("activities", []):
            fname, content = render_activity(act, ds)
            (act_dir / fname).write_text(content, encoding="utf-8")

    store_path.write_text(json.dumps(store, indent=2, default=str), encoding="utf-8")
    print(f"Written to {out_dir}")


def sink_supabase(days_data: list[dict]) -> None:
    try:
        import requests
    except ImportError:
        sys.exit("pip install requests  (needed for --sink supabase)")

    url    = os.environ["GARMIN_INGEST_URL"]
    secret = os.environ["GARMIN_INGEST_SECRET"]

    wellness   = [{k: v for k, v in d.items() if k != "activities"} for d in days_data]
    activities = [act for d in days_data for act in d.get("activities", [])]

    resp = requests.post(
        url,
        json={"wellness": wellness, "activities": activities},
        headers={"Authorization": f"Bearer {secret}"},
        timeout=30,
    )
    resp.raise_for_status()
    print("Sent to", url, "—", resp.status_code)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Sync Garmin data to markdown/JSON or a database.")
    p.add_argument("--login",  action="store_true", help="One-time login; saves token locally and prints base64 bundle")
    p.add_argument("--days",   type=int, default=1, help="How many past days to fetch (default: 1)")
    p.add_argument("--sink",   choices=["files", "supabase"], default="files")
    p.add_argument("--out",    default="./garmin", help="Output folder for --sink files")
    args = p.parse_args()

    if args.login:
        do_login()
        return

    client = get_client()
    today  = date.today()
    days_data = []

    for i in range(args.days):
        day = today - timedelta(days=i)
        print(f"Fetching {day} …", end=" ", flush=True)
        data = fetch_day(client, day)
        days_data.append(data)
        print("done")

    if args.sink == "files":
        sink_files(days_data, Path(args.out))
    else:
        sink_supabase(days_data)


if __name__ == "__main__":
    main()
