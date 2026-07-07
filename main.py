import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

import audible

from exist_io_core_client.exist_client import ExistClient


BASE_DIR = Path(__file__).resolve().parent
AUDIBLE_AUTH_FILE = BASE_DIR / "audible_auth.json"
EXIST_OAUTH_FILE = BASE_DIR / "exist_oauth.json"
EXIST_REDIRECT_URI = "http://localhost:8000/"
EXIST_SCOPE = "media_write"
EXIST_ATTRIBUTE_DEFINITIONS = [
    {
        "key": "audible_listening_minutes",
        "label": "Audible listening",
        "group": "media",
        "value_type": 3,
        "manual": False,
    },
    {
        "key": "audible_books_finished",
        "label": "Audible books finished",
        "group": "media",
        "value_type": 0,
        "manual": False,
    },
]


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or default


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def audible_locale() -> str:
    return required_env("AUDIBLE_LOCALE")


def exist_client_id() -> str:
    return required_env("EXIST_CLIENT_ID")


def exist_client_secret() -> str:
    return required_env("EXIST_CLIENT_SECRET")


def exist_client() -> ExistClient:
    return ExistClient(
        token_file=EXIST_OAUTH_FILE,
        client_id=exist_client_id(),
        client_secret=exist_client_secret(),
        redirect_uri=EXIST_REDIRECT_URI,
        scope=EXIST_SCOPE,
    )


def user_today() -> str:
    return dt.datetime.now().astimezone().date().isoformat()


def day_start_utc(day: str) -> str:
    local_day = dt.date.fromisoformat(day)
    local_timezone = dt.datetime.now().astimezone().tzinfo
    if local_timezone is None:
        raise RuntimeError("Could not determine the system local timezone.")
    local_midnight = dt.datetime.combine(local_day, dt.time.min, tzinfo=local_timezone)
    return local_midnight.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def day_bounds_utc(day: str) -> tuple[dt.datetime, dt.datetime]:
    local_day = dt.date.fromisoformat(day)
    local_timezone = dt.datetime.now().astimezone().tzinfo
    if local_timezone is None:
        raise RuntimeError("Could not determine the system local timezone.")

    day_start_local = dt.datetime.combine(local_day, dt.time.min, tzinfo=local_timezone)
    next_day_start_local = day_start_local + dt.timedelta(days=1)
    return (
        day_start_local.astimezone(dt.timezone.utc),
        next_day_start_local.astimezone(dt.timezone.utc),
    )


def resolve_day(day: str | None = None) -> str:
    if day:
        return dt.date.fromisoformat(day).isoformat()
    return user_today()


def recent_days(limit: int) -> list[str]:
    if limit < 1:
        raise RuntimeError("Day limit must be at least 1.")

    today = dt.datetime.now().astimezone().date()
    return [(today - dt.timedelta(days=offset)).isoformat() for offset in range(0, limit)]


def login_to_exist() -> None:
    client = exist_client()
    client.login()


def load_auth() -> audible.Authenticator:
    if not AUDIBLE_AUTH_FILE.exists():
        raise RuntimeError(
            f"Missing Audible auth file at {AUDIBLE_AUTH_FILE}. Run `python main.py auth` first."
        )
    return audible.Authenticator.from_file(str(AUDIBLE_AUTH_FILE))


def save_auth(auth: audible.Authenticator) -> None:
    auth.to_file(str(AUDIBLE_AUTH_FILE), encryption=False)


def login_to_audible() -> None:
    auth = audible.Authenticator.from_login_external(locale=audible_locale())
    save_auth(auth)
    logging.info("Saved Audible auth to %s", AUDIBLE_AUTH_FILE)


def audible_client() -> audible.Client:
    return audible.Client(auth=load_auth())


def fetch_library(client: audible.Client) -> list[dict[str, Any]]:
    response = client.get(
        "1.0/library",
        num_results=1000,
        response_groups="product_desc,product_attrs,is_finished,listening_status,percent_complete",
        sort_by="-PurchaseDate",
    )
    return response.get("items", [])


def fetch_daily_stats(client: audible.Client, day: str) -> dict[str, Any]:
    month = day[:7]
    return client.get(
        "1.0/stats/aggregates",
        monthly_listening_interval_duration=0,
        monthly_listening_interval_start_date=month,
        daily_listening_interval_duration=1,
        daily_listening_interval_start_date=day,
        response_groups="total_listening_stats",
        store="Audible",
        locale=audible_locale(),
    )


def walk_values(node: Any) -> list[Any]:
    values: list[Any] = []
    if isinstance(node, dict):
        for value in node.values():
            values.extend(walk_values(value))
    elif isinstance(node, list):
        for item in node:
            values.extend(walk_values(item))
    else:
        values.append(node)
    return values


def extract_minutes(stats_json: dict[str, Any], target_day: str) -> int:
    daily_stats = stats_json.get("aggregated_daily_listening_stats")
    if isinstance(daily_stats, list):
        if not daily_stats:
            logging.info("Audible returned no daily listening rows; defaulting to 0 minutes for %s.", target_day)
            return 0

        for item in daily_stats:
            if not isinstance(item, dict):
                continue
            if item.get("interval_identifier") != target_day:
                continue

            aggregated_sum = item.get("aggregated_sum")
            unit = str(item.get("unit", "")).lower()
            if not isinstance(aggregated_sum, (int, float)):
                continue

            if unit == "milliseconds":
                minutes = int(round(float(aggregated_sum) / 60000))
            elif unit == "seconds":
                minutes = int(round(float(aggregated_sum) / 60))
            else:
                minutes = int(round(float(aggregated_sum)))

            if 0 <= minutes <= 1440:
                return minutes

        logging.info(
            "Audible returned daily listening rows, but none matched %s; defaulting to 0 minutes.",
            target_day,
        )
        return 0

    candidates: list[tuple[int, int, str]] = []

    def normalize_candidate(path: str, value: float) -> int | None:
        normalized = path.lower()

        if "millisecond" in normalized or normalized.endswith("_ms") or normalized.endswith("ms"):
            minutes = value / 60000
        elif "second" in normalized or normalized.endswith("_sec") or normalized.endswith("secs") or normalized.endswith("sec"):
            minutes = value / 60
        elif "minute" in normalized or normalized.endswith("_min") or normalized.endswith("mins") or normalized.endswith("min"):
            minutes = value
        elif value <= 1440:
            minutes = value
        elif value <= 86400:
            minutes = value / 60
        else:
            minutes = value / 60000

        rounded = int(round(minutes))
        if 0 <= rounded <= 1440:
            return rounded
        return None

    def visit(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = f"{path}.{key}" if path else key
                if isinstance(value, (int, float)):
                    minutes = normalize_candidate(next_path, float(value))
                    if minutes is not None:
                        score = 0
                        normalized = next_path.lower()
                        if "daily" in normalized:
                            score += 10
                        if "day" in normalized:
                            score += 5
                        if "monthly" in normalized:
                            score -= 5
                        if "month" in normalized:
                            score -= 3
                        if "total_listening" in normalized:
                            score += 2
                        if "minute" in normalized or "second" in normalized or "millisecond" in normalized:
                            score += 2
                        candidates.append((score, minutes, next_path))
                visit(value, next_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")

    visit(stats_json)
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_score, best_minutes, best_path = candidates[0]
        logging.info("Using Audible stats field %s=%s minutes (score=%s)", best_path, best_minutes, best_score)
        return best_minutes

    numeric_values = [value for value in walk_values(stats_json) if isinstance(value, (int, float))]
    if len(numeric_values) == 1:
        fallback = normalize_candidate("unknown", float(numeric_values[0]))
        if fallback is not None:
            return fallback

    raise RuntimeError(
        "Could not determine listening minutes from Audible stats response. "
        "Run `python main.py inspect --date YYYY-MM-DD` and inspect the response shape."
    )


def fetch_finished_raw(client: audible.Client, day: str) -> Any:
    start_date = day_start_utc(day)
    return client.get("1.0/stats/status/finished", start_date=start_date)


def parse_rfc3339_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def count_finished_events_in_day(response: Any, day: str) -> int:
    day_start, next_day_start = day_bounds_utc(day)

    if isinstance(response, list):
        events = response
    elif isinstance(response, dict):
        events = response.get("mark_as_finished_status_list", [])
    else:
        events = []

    if not isinstance(events, list):
        return 0

    finished_count = 0
    for item in events:
        if not isinstance(item, dict):
            continue
        if item.get("is_marked_as_finished") is not True:
            continue

        event_timestamp = item.get("event_timestamp")
        if not isinstance(event_timestamp, str) or not event_timestamp:
            continue

        try:
            event_time = parse_rfc3339_utc(event_timestamp)
        except ValueError:
            continue

        if day_start <= event_time < next_day_start:
            finished_count += 1

    return finished_count


def fetch_finished_count(client: audible.Client, day: str) -> int:
    response = fetch_finished_raw(client, day)
    return count_finished_events_in_day(response, day)


def build_daily_values(stats: dict[str, Any], day: str, finished_count: int) -> dict[str, Any]:
    return {
        "audible_listening_minutes": extract_minutes(stats, day),
        "audible_books_finished": finished_count,
    }


def log_daily_values(day: str, values: dict[str, Any]) -> None:
    logging.info(
        "%s -> %s listening minutes - %s book(s) finished",
        day,
        values["audible_listening_minutes"],
        values["audible_books_finished"],
    )


def sync_days(target_days: list[str]) -> None:
    exist = exist_client()
    attribute_names = exist.ensure_attributes(EXIST_ATTRIBUTE_DEFINITIONS)
    payload: list[dict[str, Any]] = []

    with audible_client() as audible_api:
        for day in target_days:
            stats = fetch_daily_stats(audible_api, day)
            finished_count = fetch_finished_count(audible_api, day)
            values = build_daily_values(stats, day, finished_count)
            log_daily_values(day, values)
            payload.extend(
                exist.build_update_payload(
                    EXIST_ATTRIBUTE_DEFINITIONS,
                    attribute_names,
                    day,
                    values,
                )
            )

    result = exist.post_updates(payload)
    logging.info(
        "Synced %s values across %s day(s) (%s to %s)",
        len(result.get("success", [])),
        len(target_days),
        target_days[-1],
        target_days[0],
    )
    if result.get("failed"):
        raise RuntimeError(f"Some Exist updates failed: {result['failed']}")


def sync(days: int = 2) -> None:
    sync_days(recent_days(days))


def inspect(day: str | None = None) -> None:
    resolved_day = resolve_day(day)
    with audible_client() as client:
        stats = fetch_daily_stats(client, resolved_day)
        finished_count = fetch_finished_count(client, resolved_day)
    print(
        json.dumps(
            {
                "date": resolved_day,
                "finished_count": finished_count,
                "stats": stats,
            },
            indent=2,
        )
    )


def inspect_library() -> None:
    with audible_client() as client:
        books = fetch_library(client)
    print(json.dumps(books[:5], indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Audible listening data into Exist.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("auth", help="Authorize Audible in your browser and save device credentials.")
    subparsers.add_parser("exist-auth", help="Authorize Exist in your browser and save OAuth tokens.")
    sync_parser = subparsers.add_parser(
        "sync",
        help="Fetch Audible data and push recent values to Exist.",
    )
    sync_parser.add_argument(
        "--days",
        type=int,
        default=2,
        help="Number of recent days to sync, counting backward from today.",
    )
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Print the Audible daily stats and finished-book count for a date.",
    )
    inspect_parser.add_argument("--date", help="Inspect a specific local date in YYYY-MM-DD format.")
    subparsers.add_parser("inspect-library", help="Print the first few Audible library items.")

    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("audible").setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "auth":
        login_to_audible()
    elif args.command == "exist-auth":
        login_to_exist()
    elif args.command == "sync":
        sync(days=args.days)
    elif args.command == "inspect":
        inspect(day=args.date)
    elif args.command == "inspect-library":
        inspect_library()
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
