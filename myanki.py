# myanki.py 

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote

from email_validator import EmailNotValidError, validate_email
from flask import Flask, Response, abort, redirect, render_template, request, send_from_directory, session, url_for
from zoneinfo import ZoneInfo
from fsrs import Card as FSRSCard, Rating, Scheduler, State
import esperanto

# Paths and application settings

BASE_DIR = Path(__file__).resolve().parent
PAGES_DIR = BASE_DIR / "static"
TEMPLATES_DIR = PAGES_DIR / "templates"
USERS_DIR = BASE_DIR / "users"

TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
USERS_DIR.mkdir(parents=True, exist_ok=True)

SESSION_SECRET = os.getenv("SESSION_SECRET", "change-this-development-secret")

CARDS_PER_SESSION_DEFAULT = 20
CARDS_PER_SESSION_MIN = 1
CARDS_PER_SESSION_MAX = 200

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_CARD_TEXT_BYTES = 16 * 1024

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=None)

app.config.update(
    SECRET_KEY=SESSION_SECRET,
    SESSION_COOKIE_NAME="memory_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_HTTPS_ONLY", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    #MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


# Deck file format

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
LOCAL_TIMEZONE = ZoneInfo("Asia/Seoul")
INITIAL_EXAM_TIME = datetime(1970, 1, 1, 0, 0, 0)

DECK_NAME_PREFIX = b"name\t"
DECK_NAME_FIELD_SIZE = 80
DECK_NAME_OFFSET = len(DECK_NAME_PREFIX)
DECK_NAME_LINE_SIZE = len(DECK_NAME_PREFIX) + DECK_NAME_FIELD_SIZE + 1

SESSION_PREFIX = b"n-session\t"
SESSION_FIELD_SIZE = 6
SESSION_OFFSET = DECK_NAME_LINE_SIZE + len(SESSION_PREFIX)
SESSION_LINE_SIZE = len(SESSION_PREFIX) + SESSION_FIELD_SIZE + 1

CARDS_START_OFFSET = DECK_NAME_LINE_SIZE + SESSION_LINE_SIZE


# Card line:
#
# 00001<TAB>1970-01-01 00:00:00<TAB>0000<TAB>0000<TAB>key<TAB>value<LF>
#
# Offset  Length  Field
# 0       5       card ID
# 5       1       TAB
# 6       19      next-exam-time
# 25      1       TAB
# 26      4       n-repetition
# 30      1       TAB
# 31      4       learned
# 35      1       TAB
# 36      ...     key/value

CARD_ID_SIZE = 5

CARD_TIME_OFFSET = 6
CARD_TIME_SIZE = 19

CARD_REPETITION_OFFSET = 26
CARD_REPETITION_SIZE = 4

CARD_LEARNED_OFFSET = 31
CARD_LEARNED_SIZE = 4

CARD_TEXT_OFFSET = 36

CARD_MUTABLE_OFFSET = CARD_TIME_OFFSET
CARD_MUTABLE_SIZE = 29


# Validation and learning constants

DECK_ID_RE = re.compile(r"^deck([1-9][0-9]*)$")

_RECALL_VALUES = {"again", "hard", "good", "easy"}

_RECALL_DELTA = {
    "again": -2,
    "hard": -1,
    "good": 1,
    "easy": 2,
}

_RECALL_LABEL = {
    "again": "again",
    "hard": "hard",
    "good": "good",
    "easy": "easy",
}

_RECALL_FACTOR = {
    "again": 0.70,
    "hard": 0.85,
    "good": 1.00,
    "easy": 1.20,
}


# Base learning intervals

_INTERVAL_TABLE = (
    (timedelta(minutes=1), timedelta(minutes=3), timedelta(minutes=5), timedelta(minutes=10)),
    (timedelta(minutes=5), timedelta(minutes=10), timedelta(minutes=40), timedelta(hours=1)),
    (timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=4), timedelta(hours=6)),
    (timedelta(minutes=20), timedelta(minutes=40), timedelta(hours=16), timedelta(days=1)),
    (timedelta(minutes=25), timedelta(minutes=50), timedelta(days=2), timedelta(days=4)),
    (timedelta(minutes=30), timedelta(minutes=60), timedelta(days=16), timedelta(days=32)),
    (timedelta(minutes=31), timedelta(minutes=62), timedelta(days=32), timedelta(days=60)),
    (timedelta(minutes=32), timedelta(minutes=64), timedelta(days=240), timedelta(days=360)),
    (timedelta(minutes=33), timedelta(minutes=66), timedelta(days=360), timedelta(days=360)),
)

RECALL_VALUES = {"again", "hard", "good", "easy"}

RECALL_LABEL = {
    "again": "again",
    "hard": "hard",
    "good": "good",
    "easy": "easy",
}

FSRS_DESIRED_RETENTION = 0.90
FSRS_MAXIMUM_INTERVAL_DAYS = 36500

FSRS_SCHEDULER = Scheduler(
    desired_retention=FSRS_DESIRED_RETENTION,
    learning_steps=(timedelta(minutes=1), timedelta(minutes=10)),
    relearning_steps=(timedelta(minutes=10),),
    maximum_interval=FSRS_MAXIMUM_INTERVAL_DAYS,
    enable_fuzzing=True,
)

FSRS_RATINGS = (Rating.Again, Rating.Hard, Rating.Good, Rating.Easy)

RATING_LEARNED_DELTA = {
    Rating.Again: -2,
    Rating.Hard: -1,
    Rating.Good: 1,
    Rating.Easy: 2,
}


# Default English deck

DEFAULT_ENGLISH_CARDS = (
    ("apple", "사과"),
    ("book", "책"),
    ("computer", "컴퓨터"),
    ("memory", "기억"),
    ("learn", "배우다"),
    ("language", "언어"),
    ("question", "질문"),
    ("answer", "대답"),
    ("morning", "아침"),
    ("evening", "저녁"),
    ("friend", "친구"),
    ("family", "가족"),
    ("water", "물"),
    ("house", "집"),
    ("school", "학교"),
    ("work", "일"),
    ("time", "시간"),
    ("today", "오늘"),
    ("tomorrow", "내일"),
    ("yesterday", "어제"),
)

@app.errorhandler(405)
def method_not_allowed(error):
    target = url_for("home") if current_email() is not None else url_for("login_page")

    if request.headers.get("HX-Request") == "true":
        response = Response(status=204)
        response.headers["HX-Redirect"] = target
        return response

    return redirect(target, code=303)

# Data structures

class DeckFormatError(ValueError):
    pass


@dataclass(slots=True)
class Profile:
    email: str
    selected_deck: str
    cards_per_session: int


@dataclass(slots=True)
class Card:
    line_offset: int
    card_id: int
    next_exam_time: datetime
    n_repetition: int
    learned: int
    key: str
    value: str


@dataclass(slots=True)
class DeckHeader:
    name: str
    n_session: int


@dataclass(slots=True)
class DeckSummary:
    deck_id: str
    name: str
    n_session: int
    card_count: int
    due_count: int
    progress_percent: float


@dataclass(slots=True)
class ImportResult:
    added: int
    duplicate: int
    rejected: int
    errors: list[str]


# General helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def local_now() -> datetime:
    return datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None, microsecond=0)


def local_naive_to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)


def utc_to_local_naive(value: datetime) -> datetime:
    if value.tzinfo is None: value = value.replace(tzinfo=timezone.utc)

    return value.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None, microsecond=0)
    
def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


def normalize_email(raw_email: str) -> str:
    try:
        result = validate_email(raw_email.strip(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc

    email = result.normalized.strip().lower()

    if any(character in email for character in "\t\r\n"): raise ValueError("The email address contains an invalid character")

    return email


def user_directory(email: str) -> Path:
    return USERS_DIR / quote(email, safe="")


def profile_path(email: str) -> Path:
    return user_directory(email) / "profile.tsv"


def validate_deck_id(deck_id: str) -> str:
    if DECK_ID_RE.fullmatch(deck_id) is None: abort(400, description="Invalid deck ID")

    return deck_id


def deck_path(email: str, deck_id: str) -> Path:
    validate_deck_id(deck_id)

    return user_directory(email) / f"{deck_id}.tsv"

def fsrs_state_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.fsrs.tsv")

def current_email() -> str | None:
    value = session.get("email")

    return value if isinstance(value, str) and value else None


def redirect_to_login() -> Response:
    return redirect(url_for("login_page"), code=303)


def render_status(template_name: str, status_code: int, **context):
    return render_template(template_name, **context), status_code


def deck_sort_key(deck_id: str) -> int:
    match = DECK_ID_RE.fullmatch(deck_id)

    return int(match.group(1)) if match is not None else 2**31 - 1


# UTF-8 and card text encoding

def encode_fixed_utf8(value: str, field_size: int) -> bytes:
    value = value.strip()

    if not value: raise ValueError("Deck name cannot be empty")

    encoded = value.encode("utf-8")

    if len(encoded) > field_size:
        encoded = encoded[:field_size]

        while encoded:
            try:
                encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]

    if not encoded: raise ValueError("Deck name cannot be encoded")

    return encoded.ljust(field_size, b" ")


def escape_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def unescape_text(value: str) -> str:
    output: list[str] = []
    index = 0

    while index < len(value):
        character = value[index]

        if character != "\\" or index + 1 >= len(value):
            output.append(character)
            index += 1
            continue

        escaped = value[index + 1]

        if escaped == "t": output.append("\t")
        elif escaped == "r": output.append("\r")
        elif escaped == "n": output.append("\n")
        elif escaped == "\\": output.append("\\")
        else:
            output.append("\\")
            output.append(escaped)

        index += 2

    return "".join(output)

# FSRS scheduler

LEGACY_STABILITY_DAYS = (0.2, 1.0, 3.0, 7.0, 14.0, 30.0, 60.0, 120.0, 240.0)


def legacy_stability_days(learned: int) -> float:
    if learned < len(LEGACY_STABILITY_DAYS): return LEGACY_STABILITY_DAYS[max(0, learned)]

    exponent = min(learned - len(LEGACY_STABILITY_DAYS) + 1, 7)

    return min(float(FSRS_MAXIMUM_INTERVAL_DAYS), LEGACY_STABILITY_DAYS[-1] * (2.0 ** exponent))


def migrate_legacy_fsrs_card(card: Card, now_utc: datetime) -> FSRSCard:
    due_utc = local_naive_to_utc(card.next_exam_time)

    if card.n_repetition <= 0:
        return FSRSCard(card_id=card.card_id, due=due_utc)

    stability = legacy_stability_days(card.learned)
    difficulty = float(clamp(int(round((8.5 - min(card.learned, 8) * 0.8) * 1000)), 1000, 10000)) / 1000.0

    if card.next_exam_time <= INITIAL_EXAM_TIME: due_utc = now_utc

    last_review = due_utc - timedelta(days=stability)

    if last_review >= now_utc: last_review = now_utc - timedelta(days=max(0.01, stability))

    return FSRSCard(
        card_id=card.card_id,
        state=State.Review,
        step=None,
        stability=stability,
        difficulty=difficulty,
        due=due_utc,
        last_review=last_review,
    )


def load_latest_fsrs_card(path: Path, card: Card, now_utc: datetime) -> FSRSCard:
    state_path = fsrs_state_path(path)
    latest_card_json: str | None = None

    if state_path.is_file():
        with state_path.open("r", encoding="utf-8", newline="") as file:
            for raw_line in file:
                parts = raw_line.rstrip("\r\n").split("\t", 5)

                if len(parts) != 6: continue

                try:
                    stored_card_id = int(parts[0])
                except ValueError:
                    continue

                if stored_card_id == card.card_id: latest_card_json = parts[4]

    if latest_card_json is not None:
        try:
            fsrs_card = FSRSCard.from_json(latest_card_json)

            if fsrs_card.card_id == card.card_id: return fsrs_card
        except (TypeError, ValueError, KeyError):
            pass

    return migrate_legacy_fsrs_card(card, now_utc)


def append_fsrs_review(path: Path, card_id: int, review_datetime_utc: datetime, preview_recall: str, final_rating: Rating, card_json: str, review_log_json: str) -> None:
    review_time_local = utc_to_local_naive(review_datetime_utc).strftime(TIME_FORMAT)
    line = f"{card_id:05d}\t{review_time_local}\t{preview_recall}\t{final_rating.value}\t{card_json}\t{review_log_json}\n"

    with fsrs_state_path(path).open("a", encoding="utf-8", newline="\n") as file:
        file.write(line)
        file.flush()


def interval_label(interval: timedelta) -> str:
    seconds = max(60, int(round(interval.total_seconds())))

    if seconds < 3600: return f"{max(1, round(seconds / 60))}분 후"
    if seconds < 86400: return f"{max(1, round(seconds / 3600))}시간 후"

    return f"{max(1, round(seconds / 86400))}일 후"


def build_fsrs_options(fsrs_card: FSRSCard, review_datetime_utc: datetime) -> list[dict]:
    original_json = fsrs_card.to_json()
    options: list[dict] = []

    prefixes = ["Again", "Hard", "Good", "Easy"]
    for idx, rating in enumerate(FSRS_RATINGS):
        candidate = FSRSCard.from_json(original_json)
        next_card, review_log = FSRS_SCHEDULER.review_card(candidate, rating, review_datetime=review_datetime_utc)
        seconds = max(60, int(round((next_card.due - review_datetime_utc).total_seconds())))

        options.append({
            "rating": rating.value,
            "seconds": seconds,
            "label": interval_label(timedelta(seconds=seconds)),
            "card_json": next_card.to_json(),
            "review_log_json": review_log.to_json(),
        })

    return options


def update_learned_from_rating(learned: int, rating: Rating) -> int:
    return clamp(learned + RATING_LEARNED_DELTA[rating], 0, 9999)


def find_next_due_card(path: Path, now: datetime) -> Card | None:
    selected: Card | None = None

    for card in iter_cards(path):
        if card.next_exam_time > now: continue

        if selected is None or (card.next_exam_time, card.card_id) < (selected.next_exam_time, selected.card_id): selected = card

    return selected

# Profile storage

def save_profile(profile: Profile) -> None:
    content = f"email\t{profile.email}\nselected-deck\t{profile.selected_deck}\ncards-per-session\t{profile.cards_per_session}\n".encode("utf-8")
    path = profile_path(profile.email)

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("wb") as file:
        file.write(content)
        file.flush()


def load_profile(email: str) -> Profile:
    path = profile_path(email)

    if not path.is_file(): raise FileNotFoundError(path)

    fields: dict[str, str] = {}

    with path.open("r", encoding="utf-8", newline="") as file:
        for raw_line in file:
            line = raw_line.rstrip("\r\n")

            if not line: continue

            parts = line.split("\t", 1)

            if len(parts) == 2: fields[parts[0]] = parts[1]

    selected_deck = fields.get("selected-deck", "")

    try:
        cards_per_session = int(fields.get("cards-per-session", str(CARDS_PER_SESSION_DEFAULT)))
    except ValueError:
        cards_per_session = CARDS_PER_SESSION_DEFAULT

    return Profile(
        email=email,
        selected_deck=selected_deck,
        cards_per_session=clamp(cards_per_session, CARDS_PER_SESSION_MIN, CARDS_PER_SESSION_MAX),
    )


# Low-level deck storage

def write_all(file: BinaryIO, data: bytes) -> None:
    written = file.write(data)

    if written != len(data): raise OSError(f"Incomplete write: expected {len(data)}, wrote {written}")


def format_deck_header(deck_name: str, n_session: int = 0) -> bytes:
    if not 0 <= n_session <= 999999: raise ValueError("n_session must be between 0 and 999999")

    return DECK_NAME_PREFIX + encode_fixed_utf8(deck_name, DECK_NAME_FIELD_SIZE) + b"\n" + SESSION_PREFIX + f"{n_session:06d}".encode("ascii") + b"\n"


def format_card_line(card_id: int, key: str, value: str) -> bytes:
    if not 1 <= card_id <= 99999: raise ValueError("card_id must be between 1 and 99999")

    key_bytes = escape_text(key).encode("utf-8")
    value_bytes = escape_text(value).encode("utf-8")

    if len(key_bytes) > MAX_CARD_TEXT_BYTES: raise ValueError("Card key is too long")
    if len(value_bytes) > MAX_CARD_TEXT_BYTES: raise ValueError("Card value is too long")

    return f"{card_id:05d}".encode("ascii") + b"\t1970-01-01 00:00:00\t0000\t0000\t" + key_bytes + b"\t" + value_bytes + b"\n"


def create_deck(path: Path, deck_name: str, cards: tuple[tuple[str, str], ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("xb", buffering=0) as file:
        write_all(file, format_deck_header(deck_name))

        for card_id, (key, value) in enumerate(cards, start=1): write_all(file, format_card_line(card_id, key, value))

        file.flush()


def read_deck_header_from_file(file: BinaryIO) -> DeckHeader:
    file.seek(0)

    if file.read(len(DECK_NAME_PREFIX)) != DECK_NAME_PREFIX: raise DeckFormatError("Invalid deck name header")

    name_field = file.read(DECK_NAME_FIELD_SIZE)

    if len(name_field) != DECK_NAME_FIELD_SIZE: raise DeckFormatError("Incomplete deck name field")
    if file.read(1) != b"\n": raise DeckFormatError("Invalid deck name line ending")
    if file.read(len(SESSION_PREFIX)) != SESSION_PREFIX: raise DeckFormatError("Invalid session header")

    session_field = file.read(SESSION_FIELD_SIZE)

    if len(session_field) != SESSION_FIELD_SIZE or not session_field.isdigit(): raise DeckFormatError("Invalid session count")
    if file.read(1) != b"\n": raise DeckFormatError("Invalid session line ending")

    try:
        name = name_field.rstrip(b" ").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DeckFormatError("Deck name is not valid UTF-8") from exc

    return DeckHeader(name=name, n_session=int(session_field))


def read_deck_header(path: Path) -> DeckHeader:
    with path.open("rb") as file: return read_deck_header_from_file(file)


def rename_deck_file(path: Path, deck_name: str) -> None:
    name_field = encode_fixed_utf8(deck_name, DECK_NAME_FIELD_SIZE)

    with path.open("r+b", buffering=0) as file:
        if file.read(len(DECK_NAME_PREFIX)) != DECK_NAME_PREFIX: raise DeckFormatError("Invalid deck name header")

        file.seek(DECK_NAME_OFFSET)
        write_all(file, name_field)
        file.flush()


def update_session_count(path: Path, n_session: int) -> None:
    if not 0 <= n_session <= 999999: raise ValueError("n_session must be between 0 and 999999")

    with path.open("r+b", buffering=0) as file:
        file.seek(DECK_NAME_LINE_SIZE)

        if file.read(len(SESSION_PREFIX)) != SESSION_PREFIX: raise DeckFormatError("Invalid session header")

        file.seek(SESSION_OFFSET)
        write_all(file, f"{n_session:06d}".encode("ascii"))
        file.flush()


def increment_session_count(path: Path) -> int:
    n_session = min(read_deck_header(path).n_session + 1, 999999)

    update_session_count(path, n_session)

    return n_session


# Card parsing

def parse_card_line(line: bytes, line_offset: int) -> Card:
    if len(line) < CARD_TEXT_OFFSET + 1: raise DeckFormatError(f"Invalid card line at byte offset {line_offset}")
    if line[5:6] != b"\t": raise DeckFormatError(f"Invalid card ID separator at byte offset {line_offset}")
    if line[25:26] != b"\t": raise DeckFormatError(f"Invalid card time separator at byte offset {line_offset}")
    if line[30:31] != b"\t": raise DeckFormatError(f"Invalid repetition separator at byte offset {line_offset}")
    if line[35:36] != b"\t": raise DeckFormatError(f"Invalid learned separator at byte offset {line_offset}")

    try:
        card_id = int(line[0:5])
        next_exam_time = datetime.strptime(line[6:25].decode("ascii"), TIME_FORMAT)
        n_repetition = int(line[26:30])
        learned = int(line[31:35])
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeckFormatError(f"Invalid fixed card field at byte offset {line_offset}") from exc

    parts = line[CARD_TEXT_OFFSET:].rstrip(b"\r\n").split(b"\t", 1)

    if len(parts) != 2: raise DeckFormatError(f"Missing card key/value separator at byte offset {line_offset}")

    try:
        key = unescape_text(parts[0].decode("utf-8"))
        value = unescape_text(parts[1].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DeckFormatError(f"Invalid UTF-8 card text at byte offset {line_offset}") from exc

    return Card(
        line_offset=line_offset,
        card_id=card_id,
        next_exam_time=next_exam_time,
        n_repetition=n_repetition,
        learned=learned,
        key=key,
        value=value,
    )


def iter_cards(path: Path) -> Iterator[Card]:
    with path.open("rb") as file:
        read_deck_header_from_file(file)

        while True:
            line_offset = file.tell()
            line = file.readline()

            if not line: return
            if not line.strip(): continue

            yield parse_card_line(line, line_offset)


def read_card_at_offset(path: Path, line_offset: int, expected_card_id: int) -> Card:
    if line_offset < CARDS_START_OFFSET: raise DeckFormatError("Invalid card line offset")

    with path.open("rb") as file:
        file.seek(line_offset)
        line = file.readline()

    if not line: raise DeckFormatError("Card line no longer exists")

    card = parse_card_line(line, line_offset)

    if card.card_id != expected_card_id: raise DeckFormatError("Card ID does not match the stored line offset")

    return card


# Fixed-position card update

def format_schedule_block(next_exam_time: datetime, n_repetition: int, learned: int) -> bytes:
    n_repetition = clamp(n_repetition, 0, 9999)
    learned = clamp(learned, 0, 9999)

    block = next_exam_time.strftime(TIME_FORMAT).encode("ascii") + b"\t" + f"{n_repetition:04d}".encode("ascii") + b"\t" + f"{learned:04d}".encode("ascii")

    if len(block) != CARD_MUTABLE_SIZE: raise ValueError("Invalid schedule block length")

    return block


def update_card_schedule(path: Path, line_offset: int, expected_card_id: int, next_exam_time: datetime, n_repetition: int, learned: int) -> None:
    block = format_schedule_block(next_exam_time, n_repetition, learned)
    expected_id = f"{expected_card_id:05d}".encode("ascii")

    with path.open("r+b", buffering=0) as file:
        file.seek(line_offset)

        if file.read(CARD_ID_SIZE) != expected_id: raise DeckFormatError("Card ID does not match the stored line offset")

        file.seek(line_offset + CARD_MUTABLE_OFFSET)
        write_all(file, block)
        file.flush()


# Deck list and user initialization

def list_deck_ids(email: str) -> list[str]:
    directory = user_directory(email)

    if not directory.is_dir(): return []

    deck_ids = [path.stem for path in directory.glob("deck*.tsv") if DECK_ID_RE.fullmatch(path.stem)]
    deck_ids.sort(key=deck_sort_key)

    return deck_ids


def next_deck_id(email: str) -> str:
    existing = set(list_deck_ids(email))
    number = 1

    while f"deck{number}" in existing: number += 1

    return f"deck{number}"


def ensure_selected_deck(profile: Profile) -> Profile:
    deck_ids = list_deck_ids(profile.email)

    if profile.selected_deck in deck_ids: return profile

    profile.selected_deck = deck_ids[0] if deck_ids else ""

    save_profile(profile)

    return profile


def ensure_user(email: str) -> Profile:
    directory = user_directory(email)

    directory.mkdir(parents=True, exist_ok=True)

    path = profile_path(email)

    if not path.is_file():
        first_deck = directory / "deck1.tsv"
        second_deck = directory / "deck2.tsv"

        if not first_deck.exists():
            create_deck(first_deck, "English Words", DEFAULT_ENGLISH_CARDS)
        if not second_deck.exists():
            create_deck(second_deck, "Esperanto", esperanto.ESP)

        profile = Profile(email=email, selected_deck="deck2", cards_per_session=CARDS_PER_SESSION_DEFAULT)

        save_profile(profile)

        return profile

    profile = load_profile(email)

    if not list_deck_ids(email):
        create_deck(directory / "deck1.tsv", "English Words", DEFAULT_ENGLISH_CARDS)

        profile.selected_deck = "deck1"

        save_profile(profile)

    return ensure_selected_deck(profile)


# Deck summaries

def learned_score(learned: int) -> float:
    if learned >= 8: return 1.0
    if learned >= 7: return 0.95
    if learned >= 6: return 0.9
    if learned >= 5: return 0.8
    if learned >= 4: return 0.7
    if learned >= 3: return 0.5
    if learned >= 2: return 0.3
    if learned >= 1: return 0.1

    return 0.0


def summarize_deck(email: str, deck_id: str, now: datetime | None = None) -> DeckSummary:
    if now is None: now = local_now()

    path = deck_path(email, deck_id)
    header = read_deck_header(path)

    card_count = 0
    due_count = 0
    score = 0.0

    for card in iter_cards(path):
        card_count += 1
        score += learned_score(card.learned)

        if card.next_exam_time <= now: due_count += 1

    progress_percent = round(score / card_count * 100.0, 2) if card_count else 0.00

    return DeckSummary(
        deck_id=deck_id,
        name=header.name,
        n_session=header.n_session,
        card_count=card_count,
        due_count=due_count,
        progress_percent=progress_percent,
    )


def summarize_all_decks(email: str) -> list[DeckSummary]:
    now = local_now()

    return [summarize_deck(email, deck_id, now) for deck_id in list_deck_ids(email)]


# Card import

def append_imported_cards(path: Path, text: str) -> ImportResult:
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES: raise ValueError("Import text is too large")

    existing_keys: set[str] = set()
    max_card_id = 0

    for card in iter_cards(path):
        existing_keys.add(card.key)
        max_card_id = max(max_card_id, card.card_id)

    result = ImportResult(added=0, duplicate=0, rejected=0, errors=[])
    new_lines: list[bytes] = []

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip(): continue

        parts = raw_line.split("\t", 1)

        if len(parts) != 2:
            result.rejected += 1
            result.errors.append(f"Line {line_number}: missing tab separator")
            continue

        key = parts[0].strip()
        value = parts[1].strip()

        if not key:
            result.rejected += 1
            result.errors.append(f"Line {line_number}: empty key")
            continue

        if not value:
            result.rejected += 1
            result.errors.append(f"Line {line_number}: empty value")
            continue

        if key in existing_keys:
            result.duplicate += 1
            continue

        if max_card_id >= 99999:
            result.rejected += 1
            result.errors.append(f"Line {line_number}: card ID limit reached")
            continue

        max_card_id += 1

        try:
            line = format_card_line(max_card_id, key, value)
        except ValueError as exc:
            result.rejected += 1
            result.errors.append(f"Line {line_number}: {exc}")
            max_card_id -= 1
            continue

        existing_keys.add(key)
        new_lines.append(line)
        result.added += 1

    if new_lines:
        with path.open("ab", buffering=0) as file:
            for line in new_lines: write_all(file, line)

            file.flush()

    return result


# Scheduler

def adjusted_learned(learned: int, recall: str) -> int:
    if recall not in RECALL_VALUES: raise ValueError("Invalid recall value")

    return clamp(learned + RECALL_DELTA[recall], 0, 9999)


def round_interval(interval: timedelta) -> timedelta:
    seconds = max(60, int(interval.total_seconds()))

    if seconds < 3600: return timedelta(minutes=max(1, round(seconds / 60)))
    if seconds < 86400: return timedelta(hours=max(1, round(seconds / 3600)))

    return timedelta(days=max(1, round(seconds / 86400)))


def interval_options(card: Card, recall: str, now: datetime) -> tuple[timedelta, ...]:
    learned = adjusted_learned(card.learned, recall)
    base_intervals = INTERVAL_TABLE[min(learned, len(INTERVAL_TABLE) - 1)]
    repetition_factor = 1.0 + min(card.n_repetition, 30) * 0.01
    overdue_factor = 1.0

    if card.learned > 0 and card.next_exam_time > INITIAL_EXAM_TIME and card.next_exam_time < now:
        overdue_seconds = (now - card.next_exam_time).total_seconds()
        reference_seconds = max(base_intervals[2].total_seconds(), 60.0)

        overdue_factor += min(overdue_seconds / reference_seconds, 1.0) * 0.20

    factor = RECALL_FACTOR[recall] * repetition_factor * overdue_factor

    return tuple(round_interval(interval * factor) for interval in base_intervals)


def interval_label(interval: timedelta) -> str:
    seconds = int(interval.total_seconds())

    if seconds < 3600: return f"{max(1, seconds // 60)} min(s)"
    if seconds < 86400: return f"{max(1, seconds // 3600)} hour(s)"

    return f"{max(1, seconds // 86400)} day(s)"


def find_next_due_card(path: Path, now: datetime) -> Card | None:
    selected: Card | None = None

    for card in iter_cards(path):
        if card.next_exam_time > now: continue

        if selected is None or (card.next_exam_time, card.card_id) < (selected.next_exam_time, selected.card_id): 
            selected = card
            break

    return selected


# Study session helpers

def get_study_state() -> dict | None:
    state = session.get("study")

    return state if isinstance(state, dict) else None


def clear_study_state() -> None:
    session.pop("study", None)


def finish_study(email: str, reason: str):
    state = get_study_state()

    if state is None:
        return render_template(
            "study/complete.html",
            email=email,
            completed_count=0,
            target_count=0,
            reason=reason,
            summary=None,
        )

    deck_id = str(state.get("deck_id", ""))
    completed_count = int(state.get("completed_count", 0))
    target_count = int(state.get("target_count", 0))
    summary = None
    path = deck_path(email, deck_id)

    if completed_count > 0 and path.is_file():
        increment_session_count(path)

        summary = summarize_deck(email, deck_id)

    clear_study_state()

    return render_template(
        "study/complete.html",
        email=email,
        completed_count=completed_count,
        target_count=target_count,
        reason=reason,
        summary=summary,
    )


def render_next_question(email: str):
    state = get_study_state()

    if state is None: return render_template("study/no_due_cards.html", email=email, message="Study session has not started yet.")

    deck_id = str(state.get("deck_id", ""))
    completed_count = int(state.get("completed_count", 0))
    target_count = int(state.get("target_count", 0))

    if completed_count >= target_count: return finish_study(email, "target-reached")

    path = deck_path(email, deck_id)

    if not path.is_file():
        clear_study_state()

        return render_template("study/no_due_cards.html", email=email, message="Cannot find the deck you selected.")

    card = find_next_due_card(path, local_now())

    if card is None:
        if completed_count > 0: return finish_study(email, "no-more-due-cards")

        clear_study_state()

        return render_template("study/no_due_cards.html", email=email, message="No more cards to study.")

    state["current_card_id"] = card.card_id
    state["current_line_offset"] = card.line_offset
    state.pop("recall", None)
    state.pop("fsrs_options", None)
    state.pop("fsrs_review_datetime", None)

    session["study"] = state
    session.modified = True

    return render_template(
        "study/question.html",
        email=email,
        deck=read_deck_header(path),
        card=card,
        completed_count=completed_count,
        target_count=target_count,
    )


# Static files

@app.get("/static/<path:filename>", endpoint="static")
def pages_static(filename: str):
    top_directory = filename.split("/", 1)[0]

    if top_directory not in {"css", "js", "img"}: abort(404)

    return send_from_directory(PAGES_DIR, filename)


# Authentication routes

@app.get("/")
def root():
    return redirect(url_for("home" if current_email() is not None else "login_page"), code=303)


@app.get("/login")
def login_page():
    if current_email() is not None: return redirect(url_for("home"), code=303)

    return render_template("login.html", error=None)


@app.post("/login")
def login():
    raw_email = request.form.get("email", "")

    try:
        email = normalize_email(raw_email)
        ensure_user(email)
    except ValueError as exc:
        return render_status("login.html", 400, error=str(exc), email_value=raw_email)

    session.clear()
    session["email"] = email
    session.permanent = True

    return redirect(url_for("home"), code=303)


@app.post("/logout")
def logout():
    session.clear()

    return redirect(url_for("login_page"), code=303)


# Home routes

@app.get("/home")
def home():
    email = current_email()

    if email is None: return redirect_to_login()

    profile = ensure_selected_deck(load_profile(email))
    summary = None

    if profile.selected_deck and deck_path(email, profile.selected_deck).is_file(): summary = summarize_deck(email, profile.selected_deck)

    return render_template("home.html", email=email, profile=profile, summary=summary)


@app.get("/api/home/summary")
def home_summary():
    email = current_email()

    if email is None: return redirect_to_login()

    profile = ensure_selected_deck(load_profile(email))
    summary = None

    if profile.selected_deck and deck_path(email, profile.selected_deck).is_file(): summary = summarize_deck(email, profile.selected_deck)

    return render_template("fragments/home_summary.html", email=email, profile=profile, summary=summary)


# Settings routes

@app.get("/settings")
def settings_page():
    email = current_email()

    if email is None: return redirect_to_login()

    profile = ensure_selected_deck(load_profile(email))

    return render_template("settings.html", email=email, profile=profile, decks=summarize_all_decks(email))


@app.post("/api/settings/cards-per-session")
def set_cards_per_session():
    email = current_email()

    if email is None: return redirect_to_login()

    cards_per_session = request.form.get("cards_per_session", type=int)

    if cards_per_session is None or not CARDS_PER_SESSION_MIN <= cards_per_session <= CARDS_PER_SESSION_MAX:
        return render_status(
            "fragments/settings_saved.html",
            400,
            ok=False,
            message=f"Type a value in between {CARDS_PER_SESSION_MIN}~{CARDS_PER_SESSION_MAX}.",
        )

    profile = load_profile(email)
    profile.cards_per_session = cards_per_session

    save_profile(profile)

    return render_template("fragments/settings_saved.html", ok=True, message="Successfully stored.")


# Deck route helpers

def render_deck_list(email: str, message: str | None = None, error: str | None = None):
    profile = ensure_selected_deck(load_profile(email))

    return render_template(
        "fragments/deck_list.html",
        email=email,
        profile=profile,
        decks=summarize_all_decks(email),
        message=message,
        error=error,
    )


# Deck routes

@app.get("/api/decks")
def deck_list():
    email = current_email()

    if email is None: return redirect_to_login()

    return render_deck_list(email)


@app.post("/api/decks")
def create_deck_route():
    email = current_email()

    if email is None: return redirect_to_login()

    try:
        deck_id = next_deck_id(email)

        create_deck(deck_path(email, deck_id), request.form.get("deck_name", ""))
    except (ValueError, OSError) as exc:
        return render_deck_list(email, error=str(exc))

    profile = load_profile(email)

    if not profile.selected_deck:
        profile.selected_deck = deck_id

        save_profile(profile)

    return render_deck_list(email, message="Created a deck.")


@app.patch("/api/decks/<deck_id>")
def rename_deck_route(deck_id: str):
    email = current_email()

    if email is None: return redirect_to_login()

    path = deck_path(email, deck_id)

    if not path.is_file(): return render_deck_list(email, error="Cannot find a deck.")

    try:
        rename_deck_file(path, request.form.get("deck_name", ""))
    except (ValueError, OSError, DeckFormatError) as exc:
        return render_deck_list(email, error=str(exc))

    return render_deck_list(email, message="Deck name changed.")


@app.delete("/api/decks/<deck_id>")
def delete_deck_route(deck_id: str):
    email = current_email()

    if email is None: return redirect_to_login()

    path = deck_path(email, deck_id)

    if path.is_file(): path.unlink()
    
    state_path = fsrs_state_path(path)
    
    if state_path.is_file(): state_path.unlink()

    profile = load_profile(email)

    if profile.selected_deck == deck_id:
        deck_ids = list_deck_ids(email)
        profile.selected_deck = deck_ids[0] if deck_ids else ""

        save_profile(profile)

    state = get_study_state()

    if state is not None and state.get("deck_id") == deck_id: clear_study_state()

    return render_deck_list(email, message="Deleted the deck.")


@app.post("/api/decks/<deck_id>/select")
def select_deck_route(deck_id: str):
    email = current_email()

    if email is None: return redirect_to_login()

    path = deck_path(email, deck_id)

    if not path.is_file(): return render_deck_list(email, error="Cannot find the deck.")

    profile = load_profile(email)
    profile.selected_deck = deck_id

    save_profile(profile)
    clear_study_state()

    return render_deck_list(email, message="Selected the deck to study.")


# Card import routes

@app.get("/cards/add")
def add_cards_page():
    email = current_email()

    if email is None: return redirect_to_login()

    profile = ensure_selected_deck(load_profile(email))

    return render_template("cards_add.html", email=email, profile=profile, decks=summarize_all_decks(email))


@app.post("/api/decks/<deck_id>/cards/import")
def import_cards_route(deck_id: str):
    email = current_email()

    if email is None: return redirect_to_login()

    path = deck_path(email, deck_id)

    if not path.is_file():
        return render_status(
            "fragments/import_result.html",
            404,
            ok=False,
            message="Cannot find the deck.",
            result=None,
        )

    try:
        result = append_imported_cards(path, request.form.get("cards_text", ""))
    except (ValueError, OSError, DeckFormatError) as exc:
        return render_status(
            "fragments/import_result.html",
            400,
            ok=False,
            message=str(exc),
            result=None,
        )

    return render_template(
        "fragments/import_result.html",
        ok=True,
        message="Completed importing cards.",
        result=result,
    )


# Study routes

@app.post("/study/start")
def start_study():
    email = current_email()

    if email is None: return redirect_to_login()

    profile = ensure_selected_deck(load_profile(email))

    if not profile.selected_deck: return render_template("study/no_due_cards.html", email=email, message="Create a deck first.")

    path = deck_path(email, profile.selected_deck)

    if not path.is_file(): return render_template("study/no_due_cards.html", email=email, message="Cannot find the selected deck.")

    session["study"] = {
        "deck_id": profile.selected_deck,
        "target_count": profile.cards_per_session,
        "completed_count": 0,
    }

    session.modified = True

    return render_next_question(email)


@app.get("/study/question")
def study_question():
    email = current_email()

    if email is None: return redirect_to_login()

    return render_next_question(email)


@app.post("/study/reveal")
def reveal_answer():
    email = current_email()

    if email is None: return redirect_to_login()

    card_id = request.form.get("card_id", type=int)
    recall = request.form.get("recall", "")

    if card_id is None or recall not in RECALL_VALUES: abort(400, description="Invalid study answer")

    state = get_study_state()

    if state is None: return render_next_question(email)

    expected_card_id = int(state.get("current_card_id", -1))
    line_offset = int(state.get("current_line_offset", -1))

    if card_id != expected_card_id: abort(409, description="Study card changed")

    deck_id = str(state.get("deck_id", ""))
    path = deck_path(email, deck_id)
    card = read_card_at_offset(path, line_offset, expected_card_id)
    review_datetime_utc = utc_now()
    fsrs_card = load_latest_fsrs_card(path, card, review_datetime_utc)
    fsrs_options = build_fsrs_options(fsrs_card, review_datetime_utc)

    state["recall"] = recall
    state["fsrs_options"] = fsrs_options
    state["fsrs_review_datetime"] = review_datetime_utc.isoformat()

    session["study"] = state
    session.modified = True

    answer_titles = ("Again", "Hard", "Good", "Easy")
    choices = [
        {
            "index": index,
            "seconds": option["seconds"],
            "title": answer_titles[index],
            "label": option["label"],
        }
        for index, option in enumerate(fsrs_options)
    ]

    return render_template(
        "study/answer.html",
        email=email,
        deck=read_deck_header(path),
        card=card,
        recall=recall,
        recall_label=RECALL_LABEL[recall],
        choices=choices,
        completed_count=int(state.get("completed_count", 0)),
        target_count=int(state.get("target_count", 0)),
    )

@app.post("/study/schedule")
def schedule_card():
    email = current_email()

    if email is None: return redirect_to_login()

    card_id = request.form.get("card_id", type=int)
    recall = request.form.get("recall", "")
    interval_index = request.form.get("interval_index", type=int)

    if card_id is None or interval_index is None: abort(400, description="Invalid study schedule")

    state = get_study_state()

    if state is None: return render_next_question(email)

    expected_card_id = int(state.get("current_card_id", -1))
    line_offset = int(state.get("current_line_offset", -1))
    expected_recall = state.get("recall")
    fsrs_options = state.get("fsrs_options")
    review_datetime_text = state.get("fsrs_review_datetime")

    if card_id != expected_card_id or recall != expected_recall: abort(409, description="Study answer changed")
    if not isinstance(fsrs_options, list) or len(fsrs_options) != 4: abort(409, description="FSRS options are missing")
    if not isinstance(review_datetime_text, str): abort(409, description="FSRS review time is missing")
    if not 0 <= interval_index < len(fsrs_options): abort(400, description="Invalid interval index")

    selected = fsrs_options[interval_index]

    if not isinstance(selected, dict): abort(409, description="Invalid FSRS option")

    try:
        final_rating = Rating(int(selected["rating"]))
        next_fsrs_card = FSRSCard.from_json(str(selected["card_json"]))
        review_log_json = str(selected["review_log_json"])
        review_datetime_utc = datetime.fromisoformat(review_datetime_text)
    except (KeyError, TypeError, ValueError):
        abort(409, description="Invalid FSRS state")

    if final_rating.value != interval_index + 1: abort(409, description="FSRS rating does not match the selected option")
    if next_fsrs_card.card_id != expected_card_id: abort(409, description="FSRS card ID changed")
    if review_datetime_utc.tzinfo is None: review_datetime_utc = review_datetime_utc.replace(tzinfo=timezone.utc)

    deck_id = str(state.get("deck_id", ""))
    path = deck_path(email, deck_id)
    card = read_card_at_offset(path, line_offset, expected_card_id)
    next_exam_time = utc_to_local_naive(next_fsrs_card.due)
    n_repetition = min(card.n_repetition + 1, 9999)
    learned = update_learned_from_rating(card.learned, final_rating)

    update_card_schedule(path, line_offset, expected_card_id, next_exam_time, n_repetition, learned)
    append_fsrs_review(path, expected_card_id, review_datetime_utc, recall, final_rating, next_fsrs_card.to_json(), review_log_json)

    state["completed_count"] = int(state.get("completed_count", 0)) + 1
    state.pop("current_card_id", None)
    state.pop("current_line_offset", None)
    state.pop("recall", None)
    state.pop("fsrs_options", None)
    state.pop("fsrs_review_datetime", None)

    session["study"] = state
    session.modified = True

    return render_next_question(email)
    
@app.post("/study/stop")
def stop_study():
    email = current_email()

    if email is None: return redirect_to_login()

    state = get_study_state()

    if state is None: return redirect(url_for("home"), code=303)
    if int(state.get("completed_count", 0)) > 0: return finish_study(email, "stopped")

    clear_study_state()

    return redirect(url_for("home"), code=303)


# Development entry point

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8000,
        debug=True,
        threaded=True,
        processes=1,
    )

# eof 