#!/usr/bin/env python3
"""
Ingest emails from an IMAP mailbox into Graphiti.

ONE episode per email thread (subject normalized). Threads grouped by
References / In-Reply-To headers when present, falling back to normalized
subject (Re:/Fwd: stripped). Each episode renders all messages in
chronological order with from, date, body (HTML stripped to plain text).

Filters:
- --addresses — comma-separated allowlist matched against EVERY participant
  (From/To/Cc/Bcc/Reply-To). Each entry is either a full address
  (alice@client.com) or a domain match (@client.com matches anyone at that
  domain). Run client-side after the IMAP fetch — covers Cc/Bcc reliably.
- --require-relevance — refuse to run unless --addresses OR --include-keywords
  is set. Safety against accidentally ingesting an entire mailbox.
- --since YYYY-MM-DD — IMAP server-side SINCE filter
- --folder — default INBOX; can be 'all' for Gmail's All Mail (use '[Gmail]/All Mail')
- --include-keywords / --exclude-keywords — drop threads that don't mention
  project terms / contain excluded terms (case-insensitive substring)
- --min-words N — drop messages under N words (filters auto-reply noise)
- --min-thread-messages N — drop threads with fewer than N surviving messages

Batching (for large mailboxes):
- --batch-days N — split [since, today] into N-day windows; each window
  uses its own IMAP session. Default 30 days. Without this, big mailboxes
  (Zoho closes the socket at ~2000-3000 messages, Gmail similar) fail mid-fetch.
- --parallel-workers N — run that many batch windows in parallel; each
  worker opens its own IMAP connection. Default 1. Bump to 2-4 for big
  back-fills. Respect your provider's concurrent-connection limit (Zoho:
  ~5; Gmail: ~15; Fastmail: ~2 per IP).

Auth: password is read from env var (--password-env IMAP_PASSWORD by default)
to avoid leaking it on the CLI / in process listings. Use an app password,
not your account password, for Gmail / Microsoft etc.

Citations: source_description = `mid:<Message-ID>` (RFC 2392 — opens in
mail clients that respect the URI scheme). Also includes folder UID for
fallback identification.

Usage:
    IMAP_PASSWORD='app-password-here' python ingest_email.py \\
        --url http://localhost:8765/mcp \\
        --group-id <project-id> \\
        --imap-host imap.gmail.com \\
        --imap-user me@example.com \\
        --addresses '@client.com,external-consultant@vendor.com' \\
        --require-relevance \\
        --since 2024-01-01 \\
        --include-keywords 'projectname,ticket-prefix' \\
        [--folder INBOX] [--dry-run] [--reingest]
"""

from __future__ import annotations

import argparse
import datetime
import email
import email.header
import email.utils
import hashlib
import html.parser
import imaplib
import json
import os
import re
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from graphiti_client import GraphitiClient, GraphitiError

STATE_FILE = ".pb-graphiti-ingest.json"
MAX_EPISODE_CHARS = 100000  # Raised from 30k for consistency with ingest_tickets
# after a live case (pvc #361) showed long ticket threads were truncated mid-
# discussion, chopping the highest-value extractable content. Email threads
# carry the same risk — contract negotiations, vendor support threads, and
# project-status chains routinely exceed 30k. 100k handles all but the
# longest threads while keeping per-thread Haiku cost bounded (~$0.005-0.015
# per call in practice).


class HTMLStripper(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0  # nesting depth inside <script>/<style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("p", "br", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip == 0:
            self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)


def html_to_text(html: str) -> str:
    s = HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        # Malformed HTML — fall back to a brute regex strip.
        return re.sub(r"<[^>]+>", " ", html)
    text = s.get_text()
    # Collapse runs of whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def decode_header_value(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
    except Exception:
        return raw
    out: list[str] = []
    for p, enc in parts:
        if isinstance(p, bytes):
            try:
                out.append(p.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(p.decode("utf-8", errors="replace"))
        else:
            out.append(p)
    return "".join(out).strip()


def load_state(state_path: Path) -> dict[str, list[str]]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except json.JSONDecodeError:
            print(f"WARN: corrupt state file {state_path}, starting fresh", file=sys.stderr)
    return {}


def save_state(state_path: Path, state: dict[str, list[str]]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def normalize_subject(subj: str) -> str:
    """Strip Re:/Fwd: prefixes for subject-based thread grouping."""
    s = subj.strip()
    while True:
        m = re.match(r"^(re|fwd|fw|aw|sv|odp|tr|wg)\s*:\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end():].strip()
    return s.lower()


def extract_body(msg: email.message.Message) -> str:
    """Get the best text body from a MIME message."""
    if msg.is_multipart():
        # Prefer text/plain, fall back to text/html
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            return html_to_text("\n".join(html_parts))
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if not payload:
            return ""
        charset = msg.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            text = html_to_text(text)
        return text.strip()


def quote_trim(text: str) -> str:
    """Drop quoted-reply lines (lines starting with `>`) to reduce duplication."""
    out: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith(">"):
            continue
        # Crude "On <date>, <author> wrote:" stripper — common reply marker.
        if re.match(r"^\s*On .* wrote:\s*$", line):
            break
        out.append(line)
    return "\n".join(out).strip()


def address_matches(haystack: str, needle: str) -> bool:
    if not haystack or not needle:
        return False
    return needle.lower() in haystack.lower()


def message_participants(msg: email.message.Message) -> set[str]:
    """Extract every email address mentioned in From/To/Cc/Bcc/Reply-To.

    Returns lowercased addresses (no display names).
    """
    out: set[str] = set()
    for field in ("From", "To", "Cc", "Bcc", "Reply-To", "Sender"):
        raw = msg.get(field)
        if not raw:
            continue
        try:
            for _name, addr in email.utils.getaddresses([raw]):
                if addr:
                    out.add(addr.lower())
        except (TypeError, ValueError, IndexError):
            continue
    return out


def passes_address_gate(participants: set[str], allowed: set[str] | None) -> bool:
    """Match a message against an allowed-address list.

    Each allowed entry is either:
    - a full address ('alice@example.com') — matched exactly (case-insensitive)
    - a domain match ('@example.com') — matched as suffix on the participant
    """
    if not allowed:
        return True
    for participant in participants:
        for entry in allowed:
            if entry.startswith("@"):
                # Domain match: participant ends with the entry
                if participant.endswith(entry):
                    return True
            else:
                if participant == entry:
                    return True
    return False


def passes_keyword_gate(text: str, include: set[str] | None, exclude: set[str] | None) -> bool:
    lower = text.lower()
    if include and not any(k.lower() in lower for k in include):
        return False
    if exclude and any(k.lower() in lower for k in exclude):
        return False
    return True


# Built-in noise patterns — bounce daemons, uptime monitors, autoresponders,
# CI/SCM bots, calendar systems. These produce high-volume low-signal episodes
# that just pollute recall and burn Haiku credits. Override via
# --no-default-noise-filter for "give me literally everything."
DEFAULT_EXCLUDE_FROM = {
    "mailer-daemon",
    "postmaster@",
    "noreply@uptimerobot.com",
    "@uptimerobot.com",
    "@bounces.",
    "noreply@github.com",
    "notifications@github.com",
    "no-reply@",
    "noreply@",
    "donotreply@",
    "do-not-reply@",
    "@bounce.",
    "@notifications.",
}
DEFAULT_EXCLUDE_SUBJECTS = {
    "monitor is down",
    "monitor is up",
    "undeliverable:",
    "delivery status notification",
    "delivery failure",
    "auto-submitted",
    "out of office",
    "automatic reply",
    "automatic response",
    "vacation autoreply",
    "calendar invite",
    "meeting invitation",
    "appointment:",
}


def passes_noise_gate(
    from_header: str,
    subject: str,
    *,
    from_patterns: set[str],
    subject_patterns: set[str],
) -> bool:
    """Drop messages from auto-systems before any extraction cost."""
    from_lower = (from_header or "").lower()
    for p in from_patterns:
        if p in from_lower:
            return False
    subj_lower = (subject or "").lower()
    for p in subject_patterns:
        if p in subj_lower:
            return False
    return True


# === Deterministic noise pattern detection (used during --dry-run) ===
# Goal: find candidate exclusion patterns the user can review BEFORE the
# live ingest, without paying any LLM cost. All rules below are pure
# string/header inspection — no Anthropic or Voyage calls.

# Username localpart fragments that strongly suggest auto-system senders.
NOISE_LOCALPART_FRAGMENTS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "do_not_reply", "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "alerts", "alert", "monitor",
    "system", "robot", "auto-", "automatic", "delivery",
}
# Domain fragments that strongly suggest auto-system senders.
NOISE_DOMAIN_FRAGMENTS = {
    "uptimerobot", "bounces.", "notifications.", "alerts.",
    "no-reply.", "noreply.", "do-not-reply.", "donotreply.",
    "system.", "automated.", "auto-", "monitor.",
}
# Well-known automated services — flag for REVIEW (not auto-suggest).
# These often have legitimate business signal (e.g., Stripe payment alerts).
KNOWN_AUTOMATED_DOMAINS = {
    "sentry.io", "sendgrid.net", "mailgun.org", "postmarkapp.com",
    "mandrillapp.com", "atlassian.net", "github.com", "gitlab.com",
    "slack.com", "asana.com", "trello.com", "circleci.com",
    "travis-ci.com", "netlify.com", "vercel.com", "datadoghq.com",
    "newrelic.com", "pagerduty.com", "opsgenie.com", "discordapp.com",
}
# Subject-line keyword patterns suggesting auto / digest / bulk content.
SUSPECT_SUBJECT_KEYWORDS = {
    "digest", "weekly summary", "daily summary", "daily report",
    "weekly report", "build #", "build failed", "build succeeded",
    "ci pipeline", "pipeline failed", "pipeline passed",
    "delivery status", "undeliverable", "automatic reply",
    "out of office", "auto-reply", "calendar invite", "meeting invitation",
    "appointment:", "subscription expires", "subscription renewed",
    "your receipt", "receipt for", "payment receipt", "invoice attached",
    "password reset", "reset your password", "verify your email",
    "security alert", "sign-in", "new sign-in",
}


def _split_addr(from_header: str) -> tuple[str, str, str]:
    """Return (full-addr, localpart, domain) lowercased. Empty strings on failure."""
    if not from_header:
        return "", "", ""
    try:
        addrs = email.utils.getaddresses([from_header])
    except (TypeError, ValueError):
        return "", "", ""
    if not addrs or not addrs[0][1]:
        return "", "", ""
    full = addrs[0][1].lower()
    if "@" not in full:
        return full, full, ""
    local, _, domain = full.partition("@")
    return full, local, domain


def _normalize_subject_for_clustering(subj: str) -> str:
    """Strip Re:/Fwd:/etc. and trim — keeps first ~80 chars for grouping."""
    if not subj:
        return ""
    s = subj
    while True:
        m = re.match(r"^(re|fwd|fw|aw|sv|odp|tr|wg)\s*:\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end():]
    return s.strip()[:80]


def detect_noise_patterns(
    messages: list[email.message.Message],
    *,
    already_excluded_from: set[str],
    already_excluded_subjects: set[str],
    cluster_threshold: int = 3,
) -> dict:
    """Walk messages, classify suspects by detection rule. Return suggestions.

    Returns a dict:
        {
            "from_addresses":    [(addr, count, example_subject), ...],
            "from_domains":      [(domain, count, example_subject), ...],
            "known_services":    [(domain, count, example_subject), ...],
            "header_noise":      [(label, count, example), ...],
            "subject_clusters":  [(prefix, count, example_full_subject), ...],
            "suspect_subjects":  [(keyword, count, example_subject), ...],
        }

    Patterns already covered by `already_excluded_*` are filtered out so
    suggestions only ever ADD to the user's current configuration.
    """
    from_addresses: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})
    from_domains: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})
    known_services: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})
    header_noise: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})
    subject_clusters: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})
    suspect_subjects: dict[str, dict] = defaultdict(lambda: {"count": 0, "example": ""})

    for msg in messages:
        from_h = decode_header_value(msg.get("From"))
        subj_h = decode_header_value(msg.get("Subject"))
        full, local, domain = _split_addr(from_h)

        # Skip messages already caught by current filter
        from_lower = (from_h or "").lower()
        subj_lower = (subj_h or "").lower()
        if any(p in from_lower for p in already_excluded_from):
            continue
        if any(p in subj_lower for p in already_excluded_subjects):
            continue

        # 1. Sender localpart noise patterns
        if local and any(frag in local for frag in NOISE_LOCALPART_FRAGMENTS):
            from_addresses[full]["count"] += 1
            if not from_addresses[full]["example"]:
                from_addresses[full]["example"] = subj_h or "(no subject)"

        # 2. Sender domain noise patterns
        if domain and any(frag in domain for frag in NOISE_DOMAIN_FRAGMENTS):
            key = "@" + domain
            from_domains[key]["count"] += 1
            if not from_domains[key]["example"]:
                from_domains[key]["example"] = subj_h or "(no subject)"

        # 3. Known automated services (flag, don't auto-suggest)
        if domain in KNOWN_AUTOMATED_DOMAINS:
            key = "@" + domain
            known_services[key]["count"] += 1
            if not known_services[key]["example"]:
                known_services[key]["example"] = subj_h or "(no subject)"

        # 4. Header-based noise signals
        auto_sub = (msg.get("Auto-Submitted") or "").strip().lower()
        if auto_sub and auto_sub != "no":
            header_noise["Auto-Submitted (auto-generated/replied)"]["count"] += 1
            if not header_noise["Auto-Submitted (auto-generated/replied)"]["example"]:
                header_noise["Auto-Submitted (auto-generated/replied)"]["example"] = f"From: {from_h} / {subj_h}"
        prec = (msg.get("Precedence") or "").strip().lower()
        if prec in {"bulk", "list", "junk"}:
            header_noise[f"Precedence: {prec}"]["count"] += 1
            if not header_noise[f"Precedence: {prec}"]["example"]:
                header_noise[f"Precedence: {prec}"]["example"] = f"From: {from_h} / {subj_h}"
        if msg.get("List-Id"):
            header_noise["List-Id present (mailing list)"]["count"] += 1
            if not header_noise["List-Id present (mailing list)"]["example"]:
                header_noise["List-Id present (mailing list)"]["example"] = f"List: {msg.get('List-Id')}"
        return_path = (msg.get("Return-Path") or "").strip()
        if return_path in {"<>", ""}:
            # Only flag if it's actually empty <> (true bounce). Empty string
            # is just missing header — common, not a signal on its own.
            if return_path == "<>":
                header_noise["Return-Path: <> (bounce)"]["count"] += 1
                if not header_noise["Return-Path: <> (bounce)"]["example"]:
                    header_noise["Return-Path: <> (bounce)"]["example"] = f"From: {from_h} / {subj_h}"

        # 5. Subject suspect-keyword detection
        for kw in SUSPECT_SUBJECT_KEYWORDS:
            if kw in subj_lower:
                suspect_subjects[kw]["count"] += 1
                if not suspect_subjects[kw]["example"]:
                    suspect_subjects[kw]["example"] = subj_h or "(no subject)"
                break  # one keyword per message — don't double-count

        # 6. Subject prefix clustering (find repeating subject patterns)
        cluster_key = _normalize_subject_for_clustering(subj_h)
        if cluster_key:
            subject_clusters[cluster_key]["count"] += 1
            if not subject_clusters[cluster_key]["example"]:
                subject_clusters[cluster_key]["example"] = subj_h or "(no subject)"

    def _top(d: dict, min_count: int = 1) -> list[tuple[str, int, str]]:
        return sorted(
            [(k, v["count"], v["example"]) for k, v in d.items() if v["count"] >= min_count],
            key=lambda x: -x[1],
        )

    return {
        "from_addresses": _top(from_addresses),
        "from_domains": _top(from_domains),
        "known_services": _top(known_services),
        "header_noise": _top(header_noise),
        # Subject clusters need >= cluster_threshold to be worth suggesting
        "subject_clusters": _top(subject_clusters, min_count=cluster_threshold),
        "suspect_subjects": _top(suspect_subjects),
    }


def write_suggestions_file(path: Path, suggestions: dict, group_id: str) -> None:
    """Emit a simple line-oriented exclude-list file the user can edit.

    Format:
      from <pattern>       # comment
      subject <pattern>    # comment
      # lines starting with # are comments
    """
    lines: list[str] = [
        f"# pb-graphiti suggested exclude list for group_id={group_id!r}",
        f"# Generated {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        "# ",
        "# Edit this file: remove patterns you DO want ingested, add patterns",
        "# you've spotted manually. Then pass to live ingest via:",
        "#   --exclude-list-file " + str(path),
        "# ",
        "# Lines: 'from <pattern>' adds to --exclude-from; 'subject <pattern>'",
        "# adds to --exclude-subjects. '#' = comment. Patterns are substring,",
        "# case-insensitive. Defaults from DEFAULT_EXCLUDE_FROM/SUBJECTS still",
        "# apply on top of this file.",
        "",
    ]

    def _section(title: str, items: list[tuple[str, int, str]], kind: str) -> None:
        if not items:
            return
        lines.append(f"# === {title} ===")
        for pattern, count, example in items:
            ex = (example[:60] + "…") if len(example) > 60 else example
            lines.append(f"{kind} {pattern}  # {count} message(s) — e.g. {ex!r}")
        lines.append("")

    _section("Sender ADDRESSES with noise-indicating localparts",
             suggestions.get("from_addresses", []), "from")
    _section("Sender DOMAINS with noise-indicating patterns",
             suggestions.get("from_domains", []), "from")
    _section("Known automated-service domains (REVIEW — may carry useful signal like billing)",
             suggestions.get("known_services", []), "from")
    _section("Subject-line keyword matches (auto / digest / bounce)",
             suggestions.get("suspect_subjects", []), "subject")
    _section("Repeating subject prefixes (clustered)",
             suggestions.get("subject_clusters", []), "subject")

    header_noise = suggestions.get("header_noise", [])
    if header_noise:
        lines.append("# === Header-based noise indicators (informational only) ===")
        lines.append("# These messages had headers like Auto-Submitted, Precedence: bulk,")
        lines.append("# List-Id, or Return-Path: <>. Consider whether the senders / subjects")
        lines.append("# above already cover them; if not, add explicit patterns from the")
        lines.append("# example lines below.")
        for label, count, example in header_noise:
            lines.append(f"# {label}: {count} message(s) — e.g. {example}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def load_exclude_list_file(path: Path) -> tuple[set[str], set[str]]:
    """Read line-oriented exclude file. Returns (from_patterns, subject_patterns).

    Format mirrors write_suggestions_file output:
      from <pattern>   # comment
      subject <pattern># comment
      # full-line comment
    """
    from_patterns: set[str] = set()
    subject_patterns: set[str] = set()
    if not path.is_file():
        return from_patterns, subject_patterns
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()  # strip inline comment
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        kind, pattern = parts[0].lower(), parts[1].strip().lower()
        if not pattern:
            continue
        if kind == "from":
            from_patterns.add(pattern)
        elif kind == "subject":
            subject_patterns.add(pattern)
    return from_patterns, subject_patterns


def fetch_uids(M: imaplib.IMAP4, search_terms: list[str]) -> list[str]:
    typ, data = M.uid("search", None, *search_terms)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].decode("ascii", errors="replace").split()


def fetch_message(M: imaplib.IMAP4, uid: str) -> email.message.Message | None:
    typ, data = M.uid("fetch", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return None
    return email.message_from_bytes(data[0][1])


def date_chunks(start: datetime.date, end: datetime.date, days: int) -> list[tuple[datetime.date, datetime.date]]:
    """Split [start, end) into chunks of `days` length. Last chunk may be shorter."""
    out: list[tuple[datetime.date, datetime.date]] = []
    cur = start
    step = datetime.timedelta(days=days)
    while cur < end:
        nxt = min(cur + step, end)
        out.append((cur, nxt))
        cur = nxt
    return out


def fmt_imap_date(d: datetime.date) -> str:
    return d.strftime("%d-%b-%Y")


def fetch_batch(
    chunk_start: datetime.date,
    chunk_end: datetime.date,
    *,
    imap_host: str,
    imap_port: int,
    imap_user: str,
    imap_password: str,
    folder: str,
    allowed_addrs: set[str] | None,
    noise_from: set[str] | None = None,
    noise_subjects: set[str] | None = None,
    log_prefix: str = "",
) -> tuple[dict[str, list[tuple[str, email.message.Message]]], dict[str, str], int, int]:
    """Open a fresh IMAP session, fetch all messages in [chunk_start, chunk_end),
    apply the address gate AND the noise gate (bounce/monitor/autoresponder),
    group by thread key.

    Returns (threads, thread_root_id_map, dropped_address, dropped_noise).

    Raises on connection / login failure so the caller can retry or surface.
    """
    M = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        M.login(imap_user, imap_password)
        typ, _ = M.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot select folder {folder!r}")

        search_terms = ["SINCE", fmt_imap_date(chunk_start), "BEFORE", fmt_imap_date(chunk_end)]
        uids = fetch_uids(M, search_terms)
        print(f"{log_prefix}[{chunk_start} → {chunk_end}] {len(uids)} UID(s)", file=sys.stderr)

        threads: dict[str, list[tuple[str, email.message.Message]]] = defaultdict(list)
        thread_root_id: dict[str, str] = {}
        dropped_address = 0
        dropped_noise = 0
        for uid in uids:
            msg = fetch_message(M, uid)
            if msg is None:
                continue
            # Noise gate first — cheaper to check than the address gate
            # (which has to parse all the address headers). Drops bounce
            # daemons, uptime monitors, autoresponders BEFORE LLM cost
            # downstream.
            if noise_from or noise_subjects:
                from_h = decode_header_value(msg.get("From"))
                subj_h = decode_header_value(msg.get("Subject"))
                if not passes_noise_gate(from_h, subj_h,
                                          from_patterns=noise_from or set(),
                                          subject_patterns=noise_subjects or set()):
                    dropped_noise += 1
                    continue
            if allowed_addrs is not None:
                participants = message_participants(msg)
                if not passes_address_gate(participants, allowed_addrs):
                    dropped_address += 1
                    continue
            refs = (msg.get("References") or "").split()
            in_reply_to = (msg.get("In-Reply-To") or "").strip()
            msg_id = (msg.get("Message-ID") or "").strip()
            if refs:
                thread_key = refs[0]
            elif in_reply_to:
                thread_key = in_reply_to
            else:
                thread_key = "subject:" + normalize_subject(decode_header_value(msg.get("Subject")))
            threads[thread_key].append((uid, msg))
            if thread_key not in thread_root_id and msg_id:
                thread_root_id[thread_key] = msg_id
        return dict(threads), thread_root_id, dropped_address, dropped_noise
    finally:
        try:
            M.close()
        except imaplib.IMAP4.error:
            pass
        try:
            M.logout()
        except imaplib.IMAP4.error:
            pass


def render_message(msg: email.message.Message, uid: str, min_words: int) -> str | None:
    from_ = decode_header_value(msg.get("From"))
    date_raw = msg.get("Date") or ""
    try:
        when = email.utils.parsedate_to_datetime(date_raw)
        date_str = when.strftime("%Y-%m-%d %H:%M") if when else date_raw
    except (TypeError, ValueError):
        date_str = date_raw

    body = extract_body(msg)
    body = quote_trim(body)
    if not body or len(body.split()) < min_words:
        return None
    return f"From: {from_}\nDate: {date_str}\n\n{body}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--group-id", required=True, help="Project id (emails are per-project) or 'fleet'")
    ap.add_argument("--imap-host", required=True)
    ap.add_argument("--imap-port", type=int, default=993)
    ap.add_argument("--imap-user", required=True)
    ap.add_argument("--password-env", default="IMAP_PASSWORD",
                    help="Env var holding the IMAP password (default IMAP_PASSWORD). Use an app password.")
    ap.add_argument("--folder", default="INBOX",
                    help='IMAP folder; use "[Gmail]/All Mail" for Gmail-wide search')
    ap.add_argument("--addresses", "--address", dest="addresses", default="",
                    help="Comma-separated address allowlist. Matched against EVERY participant (From/To/Cc/Bcc/Reply-To). "
                         "Each entry is either a full address ('alice@x.com') or a domain match ('@x.com' matches any address at that domain). "
                         "Default: no address filter. Strongly recommended for client mailboxes — see --require-relevance.")
    ap.add_argument("--require-relevance", action="store_true",
                    help="Refuse to run unless --addresses OR --include-keywords is non-empty. Safety against accidentally ingesting an entire mailbox.")
    ap.add_argument("--since", required=True, help="YYYY-MM-DD — server-side SINCE filter")
    # Content filters
    ap.add_argument("--include-keywords", default="",
                    help="Comma-separated — thread must contain at least one (case-insensitive substring)")
    ap.add_argument("--exclude-keywords", default="",
                    help="Comma-separated — drop threads containing any")
    ap.add_argument("--min-words", type=int, default=10,
                    help="Drop messages with fewer than N words (default 10 — filters auto-replies, OOO notices)")
    ap.add_argument("--min-thread-messages", type=int, default=1,
                    help="Drop threads with fewer than N surviving messages (default 1)")
    ap.add_argument("--include-code-entities", action="store_true",
                    help="Allow extraction of file paths / class names. Default OFF — code belongs in GitNexus.")
    # Noise filters — drop bounce / monitor / autoresponder messages BEFORE LLM cost
    ap.add_argument("--exclude-from", default="",
                    help="Comma-separated From-header patterns (substrings, case-insensitive) — drop messages "
                         "whose From matches any. Added on top of the default noise list (see "
                         "--no-default-noise-filter to disable defaults).")
    ap.add_argument("--exclude-subjects", default="",
                    help="Comma-separated Subject patterns (substrings, case-insensitive) — drop messages "
                         "whose Subject matches any. Added on top of the default noise list.")
    ap.add_argument("--no-default-noise-filter", action="store_true",
                    help="Disable the built-in From/Subject noise filter (bounce daemons, uptime monitors, "
                         "OOO autoresponders, calendar invites). Default OFF — defaults ARE applied. Turn ON "
                         "if you need EVERY message including auto-notifications (rare).")
    ap.add_argument("--exclude-list-file", default=None,
                    help="Path to a line-oriented exclude file (format: 'from <pattern>' / 'subject <pattern>' / "
                         "'#comment'). Patterns ADD to --exclude-from / --exclude-subjects + defaults. The dry-run "
                         "writes a suggested file to <state-dir>/suggested-excludes-<group>.txt for you to review "
                         "and edit before live ingest.")
    # Batching (for large mailboxes — Zoho/Gmail close sockets on long fetches)
    ap.add_argument("--batch-days", type=int, default=30,
                    help="Split [--since, today] into N-day windows; each window uses its own IMAP session. "
                         "Default 30. Lower for mailboxes with very high message density.")
    ap.add_argument("--parallel-workers", type=int, default=1,
                    help="Run that many batch windows concurrently (each opens its own IMAP connection). "
                         "Default 1. Bump to 2-4 for big back-fills. Respect your provider's concurrent "
                         "connection cap (Zoho ~5, Gmail ~15, Fastmail ~2/IP).")
    # IO / dedupe
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reingest", action="store_true")
    ap.add_argument("--state-file", default=STATE_FILE)
    args = ap.parse_args()

    password = os.environ.get(args.password_env)
    if not password:
        print(f"ERROR: ${args.password_env} not set. Use an app password.", file=sys.stderr)
        return 2

    try:
        since_date = datetime.date.fromisoformat(args.since)
    except ValueError:
        print(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}", file=sys.stderr)
        return 2

    include_kw = {k.strip() for k in args.include_keywords.split(",") if k.strip()} or None
    exclude_kw = {k.strip() for k in args.exclude_keywords.split(",") if k.strip()} or None
    allowed_addrs = {a.strip().lower() for a in args.addresses.split(",") if a.strip()} or None

    if args.require_relevance and not allowed_addrs and not include_kw:
        print("ERROR: --require-relevance is set but neither --addresses nor --include-keywords given. "
              "Refusing to ingest without a relevance gate.", file=sys.stderr)
        return 2

    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    # Split the date range into batches. Each batch opens its own IMAP
    # session so big mailboxes (Zoho closes the socket at ~2-3k messages,
    # Gmail similar) succeed instead of mid-fetch dying.
    today = datetime.date.today() + datetime.timedelta(days=1)  # +1 so IMAP BEFORE includes today
    chunks = date_chunks(since_date, today, args.batch_days)
    print(f"splitting [{since_date} → {today}] into {len(chunks)} batch(es) of up to {args.batch_days} day(s); "
          f"workers={args.parallel_workers}", file=sys.stderr)

    threads: dict[str, list[tuple[str, email.message.Message]]] = defaultdict(list)
    thread_root_id: dict[str, str] = {}
    dropped_address = 0
    dropped_noise = 0

    # Compose noise filter sets — defaults + user overrides (unless --no-default-noise-filter)
    if args.no_default_noise_filter:
        noise_from: set[str] = set()
        noise_subjects: set[str] = set()
    else:
        noise_from = set(DEFAULT_EXCLUDE_FROM)
        noise_subjects = set(DEFAULT_EXCLUDE_SUBJECTS)
    noise_from |= {p.strip().lower() for p in args.exclude_from.split(",") if p.strip()}
    noise_subjects |= {p.strip().lower() for p in args.exclude_subjects.split(",") if p.strip()}
    # Load patterns from file if provided
    if args.exclude_list_file:
        file_from, file_subjects = load_exclude_list_file(Path(args.exclude_list_file).expanduser())
        if file_from or file_subjects:
            print(f"loaded {len(file_from)} from / {len(file_subjects)} subject pattern(s) from "
                  f"{args.exclude_list_file}", file=sys.stderr)
        noise_from |= file_from
        noise_subjects |= file_subjects

    fetch_kwargs = dict(
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        imap_user=args.imap_user,
        imap_password=password,
        folder=args.folder,
        allowed_addrs=allowed_addrs,
        noise_from=noise_from or None,
        noise_subjects=noise_subjects or None,
    )

    def merge(batch_result):
        nonlocal dropped_address, dropped_noise
        b_threads, b_root_id, b_dropped_addr, b_dropped_noise = batch_result
        for k, msgs in b_threads.items():
            threads[k].extend(msgs)
            if k not in thread_root_id and k in b_root_id:
                thread_root_id[k] = b_root_id[k]
        dropped_address += b_dropped_addr
        dropped_noise += b_dropped_noise

    try:
        if args.parallel_workers <= 1:
            for i, (s, e) in enumerate(chunks, 1):
                merge(fetch_batch(s, e, log_prefix=f"[batch {i}/{len(chunks)}] ", **fetch_kwargs))
        else:
            with ThreadPoolExecutor(max_workers=args.parallel_workers) as ex:
                future_to_chunk = {}
                for i, (s, e) in enumerate(chunks, 1):
                    fut = ex.submit(fetch_batch, s, e, log_prefix=f"[batch {i}/{len(chunks)}] ", **fetch_kwargs)
                    future_to_chunk[fut] = (s, e)
                for fut in as_completed(future_to_chunk):
                    s, e = future_to_chunk[fut]
                    try:
                        merge(fut.result())
                    except Exception as batch_err:
                        print(f"WARN: batch [{s} → {e}] failed: {batch_err}", file=sys.stderr)
    except (imaplib.IMAP4.error, OSError) as e:
        print(f"ERROR: IMAP failure: {e}", file=sys.stderr)
        return 1

    drop_notes = []
    if dropped_noise:
        drop_notes.append(f"{dropped_noise} dropped as noise (bounce/monitor/auto)")
    if dropped_address:
        drop_notes.append(f"{dropped_address} dropped by --addresses gate")
    drop_str = f"  ({'; '.join(drop_notes)})" if drop_notes else ""
    print(f"  grouped into {len(threads)} thread(s){drop_str}", file=sys.stderr)

    plan: list[dict] = []
    dropped_keyword = 0
    dropped_too_short = 0

    for thread_key, msgs in threads.items():
        # Sort messages chronologically
        msgs.sort(key=lambda pair: (
            email.utils.parsedate_to_datetime(pair[1].get("Date") or "1970-01-01") or datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        ))
        subject = decode_header_value(msgs[0][1].get("Subject")) or "(no subject)"
        rendered_parts: list[str] = []
        surviving = 0
        for uid, msg in msgs:
            line = render_message(msg, uid, args.min_words)
            if line:
                rendered_parts.append(line)
                surviving += 1
        if surviving < args.min_thread_messages:
            dropped_too_short += 1
            continue
        thread_body = "\n\n---\n\n".join(rendered_parts).strip()
        if not thread_body:
            continue
        if not passes_keyword_gate(thread_body, include=include_kw, exclude=exclude_kw):
            dropped_keyword += 1
            continue

        # Earliest date for reference_time
        try:
            ref_dt = email.utils.parsedate_to_datetime(msgs[0][1].get("Date") or "")
            ref_time = ref_dt.isoformat() if ref_dt else datetime.datetime.now(datetime.timezone.utc).isoformat()
        except (TypeError, ValueError):
            ref_time = datetime.datetime.now(datetime.timezone.utc).isoformat()

        root_mid = thread_root_id.get(thread_key) or thread_key
        # Strip <> wrapping for the mid: URI
        root_mid_clean = root_mid.strip("<> ")
        source_desc = f"mid:{root_mid_clean}"

        if len(thread_body) > MAX_EPISODE_CHARS:
            thread_body = thread_body[:MAX_EPISODE_CHARS] + f"\n\n[... truncated to {MAX_EPISODE_CHARS} chars from {len(thread_body)} total ...]"

        h = hashlib.sha256(f"email:{root_mid_clean}".encode("utf-8")).hexdigest()[:16]
        if h in seen:
            continue

        plan.append({
            "hash": h,
            "name": f"📧 {subject[:120]} ({len(msgs)} msg)",
            "body": thread_body,
            "source_description": source_desc,
            "reference_time": ref_time,
        })

    summary_parts: list[str] = []
    if dropped_keyword:
        summary_parts.append(f"{dropped_keyword} threads failed keyword gate")
    if dropped_too_short:
        summary_parts.append(f"{dropped_too_short} threads below --min-thread-messages")
    summary = (" (filtered: " + ", ".join(summary_parts) + ")") if summary_parts else ""
    print(f"plan: {len(plan)} thread(s) to write to group_id={args.group_id!r}{summary}")

    if args.dry_run:
        for ep in plan[:15]:
            print(f"  - {ep['name']} ({len(ep['body'])} chars)")
        if len(plan) > 15:
            print(f"  ... +{len(plan) - 15} more")

        # === Noise pattern scan (deterministic, no LLM) ===
        # Collect ALL surviving messages (post address+noise gate) and look
        # for patterns NOT already in the active filter. Surfaces additional
        # exclude candidates the user can review + edit before live ingest.
        all_msgs = [m for thread_msgs in threads.values() for _uid, m in thread_msgs]
        print(f"\nscanning {len(all_msgs)} surviving message(s) for additional noise patterns...", file=sys.stderr)
        suggestions = detect_noise_patterns(
            all_msgs,
            already_excluded_from=noise_from,
            already_excluded_subjects=noise_subjects,
        )

        suggestions_path = state_path.parent / f"suggested-excludes-{args.group_id}.txt"
        write_suggestions_file(suggestions_path, suggestions, args.group_id)

        # Inline summary
        total_suggested = sum(
            len(suggestions.get(k, [])) for k in
            ("from_addresses", "from_domains", "known_services", "subject_clusters", "suspect_subjects")
        )
        if total_suggested == 0 and not suggestions.get("header_noise"):
            print("\nNoise scan: no additional patterns found beyond the current filter.")
        else:
            print(f"\nNoise scan — {total_suggested} pattern(s) detected NOT in current filter:")
            for label, key in [
                ("Sender addresses", "from_addresses"),
                ("Sender domains", "from_domains"),
                ("Known automated services (review carefully — may have signal)", "known_services"),
                ("Subject keywords", "suspect_subjects"),
                ("Repeating subject prefixes", "subject_clusters"),
            ]:
                items = suggestions.get(key, [])[:5]
                if items:
                    print(f"\n  {label}:")
                    for pattern, count, example in items:
                        ex_short = (example[:60] + "…") if len(example) > 60 else example
                        print(f"    {pattern}  ({count}×, e.g. {ex_short!r})")
            header_noise = suggestions.get("header_noise", [])[:3]
            if header_noise:
                print("\n  Header indicators (informational):")
                for label, count, _ex in header_noise:
                    print(f"    {label}: {count} msg(s)")

            print(f"\nSuggestions written to: {suggestions_path}")
            print(f"Review + edit that file, then pass to the live run:")
            print(f"  --exclude-list-file {suggestions_path}")
        return 0
    if not plan:
        print("nothing to do (all up to date)")
        return 0

    client = GraphitiClient(args.url)
    try:
        client.initialize()
    except GraphitiError as e:
        print(f"ERROR initializing MCP session: {e}", file=sys.stderr)
        return 1

    # Code-entity suppression — same default as ingest_tickets.
    extract_kwargs: dict = {}
    if not args.include_code_entities:
        extract_kwargs["excluded_entity_types"] = ["Component"]
        extract_kwargs["custom_extraction_instructions"] = (
            "Extract ALL domain knowledge from this email thread. Be thorough — "
            "this graph is the project brain. Capture:\n"
            "- People: senders, recipients, clients, stakeholders, vendor contacts\n"
            "- Vendors and third-party services\n"
            "- Business features and product decisions with their rationale\n"
            "- Root causes of bugs and incidents, and how they were resolved\n"
            "- Client/customer requirements and preferences\n"
            "- Deployment procedures, sequencing constraints, and prerequisites "
            "(e.g. 'module must be disabled before running migration', "
            "'run X before enabling Y or Z will break')\n"
            "- Operational runbooks: specific commands, flags, and the order they "
            "must be run in\n"
            "- Warnings and 'do this before that' constraints\n"
            "- Rollback procedures and known failure modes\n"
            "- Configuration decisions: which config keys, what values, why\n"
            "- Integration decisions: which systems talk to which, in what order\n"
            "- Scope decisions: what was explicitly included or excluded and why\n"
            "- Contract / pricing / commercial discussion outcomes\n"
            "DO NOT extract: file paths, PHP class names, function/method "
            "names, error messages as standalone entities, URLs as entities, "
            "or generic technology nouns like 'database' or 'server'. "
            "Code-structural detail belongs in GitNexus, not Graphiti."
        )

    written = 0
    failed = 0
    for ep in plan:
        try:
            client.add_memory(
                group_id=args.group_id,
                name=ep["name"],
                episode_body=ep["body"],
                source="message",
                source_description=ep["source_description"],
                reference_time=ep["reference_time"],
                **extract_kwargs,
            )
            seen.add(ep["hash"])
            written += 1
            print(f"  + {ep['name']}")
            state[args.group_id] = sorted(seen)
            save_state(state_path, state)
        except GraphitiError as e:
            failed += 1
            print(f"  ! {ep['name']}: {e}", file=sys.stderr)

    print(f"\ndone: wrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
