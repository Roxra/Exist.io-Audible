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


def extract_minutes(stats_json: dict[str, Any]) -> int:
    daily_stats = stats_json.get("aggregated_daily_listening_stats")
    if isinstance(daily_stats, list):
        if not daily_stats:
            logging.info("Audible returned no daily listening rows; defaulting to 0 minutes for today.")
            return 0

        daily_candidates: list[int] = []
        for item in daily_stats:
            if not isinstance(item, dict):
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
                daily_candidates.append(minutes)

        if daily_candidates:
            best_daily = max(daily_candidates)
            logging.info("Using aggregated_daily_listening_stats=%s minutes", best_daily)
            return best_daily

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
        "Run `python main.py inspect-stats` and map the response shape."
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
            "value": extract_minutes(stats),
        },
        {
            "name": attribute_names["audible_books_finished"],
            "date": day,
            "value": finished_count,
        },
    ]


def post_exist_updates(payload: list[dict[str, Any]]) -> Any:
    return exist_oauth_client().post_updates(payload)


def sync_today() -> None:
    attribute_names = exist_oauth_client().ensure_attributes(EXIST_ATTRIBUTE_DEFINITIONS)
    day = user_today()

    with audible_client() as client:
        stats = fetch_daily_stats(client, day)
        finished_count = fetch_finished_count(client, day)

    payload = build_exist_payload(day, attribute_names, stats, finished_count)
    result = post_exist_updates(payload)
    logging.info("Synced %s values for %s", len(result.get("success", [])), day)
    if result.get("failed"):
        raise RuntimeError(f"Some Exist updates failed: {result['failed']}")


def inspect_stats() -> None:
    day = user_today()
    with audible_client() as client:
        stats = fetch_daily_stats(client, day)
    print(json.dumps(stats, indent=2))


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
    subparsers.add_parser("inspect-stats", help="Print the raw Audible daily stats response.")
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
    elif args.command == "inspect-stats":
        inspect_stats()
    elif args.command == "inspect-library":
        inspect_library()
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
