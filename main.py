import argparse
import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

import audible

from exist_oauth import ExistOAuthClient


BASE_DIR = Path(__file__).resolve().parent
AUDIBLE_AUTH_FILE = BASE_DIR / "audible_auth.json"
EXIST_OAUTH_FILE = BASE_DIR / "exist_oauth.json"
EXIST_REDIRECT_URI = "http://localhost:8000/"
EXIST_SCOPE = "media_write"
EXIST_MAX_UPDATE_OBJECTS = 36
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


def exist_oauth_client() -> ExistOAuthClient:
    return ExistOAuthClient(
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


def resolve_day(day: str | None = None) -> str:
    if day:
        return dt.date.fromisoformat(day).isoformat()
    return user_today()


def previous_days(limit: int, start_offset: int = 1) -> list[str]:
    if limit < 1:
        raise RuntimeError("Day limit must be at least 1.")

    today = dt.datetime.now().astimezone().date()
    return [(today - dt.timedelta(days=offset)).isoformat() for offset in range(start_offset, start_offset + limit)]


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def login_to_exist() -> None:
    exist_oauth_client().login()


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
                logging.info("Using aggregated_daily_listening_stats[%s]=%s minutes", target_day, minutes)
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


def fetch_finished_count(client: audible.Client, day: str) -> int:
    start_date = day_start_utc(day)
    response = client.get("1.0/stats/status/finished", start_date=start_date)
    if isinstance(response, list):
        return len(response)

    for key in ("items", "results", "finished_titles"):
        value = response.get(key)
        if isinstance(value, list):
            return len(value)

    return 0


def build_exist_payload(
    day: str,
    attribute_names: dict[str, str],
    stats: dict[str, Any],
    finished_count: int,
) -> list[dict[str, Any]]:
    return [
        {
            "name": attribute_names["audible_listening_minutes"],
            "date": day,
            "value": extract_minutes(stats, day),
        },
        {
            "name": attribute_names["audible_books_finished"],
            "date": day,
            "value": finished_count,
        },
    ]


def post_exist_updates(payload: list[dict[str, Any]]) -> Any:
    return exist_oauth_client().post_updates(payload)


def sync_day(day: str) -> Any:
    attribute_names = exist_oauth_client().ensure_attributes(EXIST_ATTRIBUTE_DEFINITIONS)
    with audible_client() as client:
        stats = fetch_daily_stats(client, day)
        finished_count = fetch_finished_count(client, day)

    payload = build_exist_payload(day, attribute_names, stats, finished_count)
    result = post_exist_updates(payload)
    logging.info("Synced %s values for %s", len(result.get("success", [])), day)
    if result.get("failed"):
        raise RuntimeError(f"Some Exist updates failed for {day}: {result['failed']}")
    return result


def sync_today() -> None:
    day = user_today()
    sync_day(day)


def sync_recent(days: int = 14) -> None:
    target_days = previous_days(days)
    attribute_names = exist_oauth_client().ensure_attributes(EXIST_ATTRIBUTE_DEFINITIONS)
    payload: list[dict[str, Any]] = []
    with audible_client() as client:
        for day in target_days:
            stats = fetch_daily_stats(client, day)
            finished_count = fetch_finished_count(client, day)
            payload.extend(build_exist_payload(day, attribute_names, stats, finished_count))

    successes = 0
    failed_entries: list[Any] = []
    for batch in chunked(payload, EXIST_MAX_UPDATE_OBJECTS):
        result = post_exist_updates(batch)
        successes += len(result.get("success", []))
        failed_entries.extend(result.get("failed", []))

    logging.info(
        "Synced %s values across %s days (%s to %s)",
        successes,
        len(target_days),
        target_days[-1],
        target_days[0],
    )
    if failed_entries:
        raise RuntimeError(f"Some Exist updates failed: {failed_entries}")


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
    subparsers.add_parser("sync", help="Fetch Audible data and push today's values to Exist.")
    sync_recent_parser = subparsers.add_parser(
        "sync-recent",
        help="Backfill recent days from yesterday backwards.",
    )
    sync_recent_parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Number of previous days to sync, counting backward from yesterday.",
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
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "auth":
        login_to_audible()
    elif args.command == "exist-auth":
        login_to_exist()
    elif args.command == "sync":
        sync_today()
    elif args.command == "sync-recent":
        sync_recent(days=args.days)
    elif args.command == "inspect":
        inspect(day=args.date)
    elif args.command == "inspect-library":
        inspect_library()
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
