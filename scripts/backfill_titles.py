"""Fill the HubSpot `title` property from the legacy `technology` values.

    python scripts/backfill_titles.py              # dry run: counts only, writes nothing
    python scripts/backfill_titles.py --apply      # writes title on every contact that lacks one

`technology` was imported as free text and HubSpot minted 63 dropdown options from
it - typos, duplicate spellings and resume fragments included. `config/title_map.json`
collapses all 63 onto the eight canonical titles agreed on 18 Sep 2026.

Safe by design:
  * dry run is the default; --apply is the only way to write
  * writes ONE property, `title`, and nothing else
  * skips a contact that already has a title, so re-running is harmless
  * stops on an unmapped value rather than guessing
  * `title` is not in the HubSpot webhook subscription (that watches decision_maker),
    so nothing here can trigger the agent or send an email
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from bench_outreach.common.hubspot_mcp import HubSpotMCP, HubSpotMCPError  # noqa: E402

PAGE = 100          # contacts read per search call
BATCH = 10          # contacts written per manage_crm_objects call (HubSpot's cap)


def load_map() -> dict[str, str]:
    """{legacy technology value -> canonical title}, flattened from the config."""
    raw = json.loads((REPO / "config" / "title_map.json").read_text(encoding="utf-8"))
    return {value: title
            for title, values in raw.items() if not title.startswith("_")
            for value in values}


def read_contacts(hs: HubSpotMCP) -> list[dict]:
    """Every contact, with the two properties this script cares about."""
    found, offset = [], 0
    while True:
        result = hs.call("search_crm_objects", {
            "objectType": "CONTACT",
            "properties": ["technology", "title"],
            "limit": PAGE, "offset": offset,
        })
        page = result.get("results") or result.get("objects") or []
        if not page:
            break
        found.extend(page)
        print(f"  read {len(found)}...", flush=True)
        if len(page) < PAGE:
            break
        offset += PAGE
    return found


def plan(contacts: list[dict], mapping: dict[str, str]) -> tuple[list[tuple], Counter, list[str]]:
    """Work out what would change. Nothing is written here."""
    todo, counts, unmapped = [], Counter(), []
    for c in contacts:
        props = c.get("properties") or c
        object_id = str(c.get("id") or props.get("hs_object_id") or "").strip()
        technology = (props.get("technology") or "").strip()
        already = (props.get("title") or "").strip()
        if not object_id:
            continue
        if already:
            counts["(already had a title - skipped)"] += 1
            continue
        if not technology:
            counts["(no technology recorded - skipped)"] += 1
            continue
        title = mapping.get(technology)
        if title is None:
            unmapped.append(technology)
            continue
        todo.append((object_id, technology, title))
        counts[title] += 1
    return todo, counts, unmapped


def write(hs: HubSpotMCP, todo: list[tuple]) -> int:
    written = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        hs.call("manage_crm_objects", {
            "confirmationStatus": "CONFIRMED",
            "updateRequest": {"objects": [
                {"objectType": "contacts", "objectId": int(oid),
                 "properties": {"title": title}}
                for oid, _tech, title in chunk]},
        })
        written += len(chunk)
        print(f"  wrote {written}/{len(todo)}", flush=True)
        time.sleep(0.2)          # gentle on the API; this is not a race
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write. Without this, nothing is changed.")
    args = ap.parse_args()

    mapping = load_map()
    print(f"mapping: {len(mapping)} legacy values -> "
          f"{len(set(mapping.values()))} titles\n")

    hs = HubSpotMCP()
    print("reading contacts from HubSpot...")
    try:
        contacts = read_contacts(hs)
    except HubSpotMCPError as exc:
        print(f"could not read contacts: {exc}")
        return 2
    print(f"  {len(contacts)} contacts\n")

    todo, counts, unmapped = plan(contacts, mapping)

    if unmapped:
        print("STOPPING - these technology values are not in config/title_map.json:")
        for value, n in Counter(unmapped).most_common():
            print(f"  {n:>4}  {value!r}")
        print("\nAdd them to the map and run again. Nothing was written.")
        return 1

    print(f"{'TITLE':<38} {'CONTACTS':>9}")
    print("-" * 49)
    for title, n in counts.most_common():
        print(f"{title:<38} {n:>9}")
    print("-" * 49)
    print(f"{'TO WRITE':<38} {len(todo):>9}\n")

    for oid, tech, title in todo[:5]:
        print(f"  sample  {oid}  {tech!r}  ->  {title!r}")
    print()

    if not args.apply:
        print("DRY RUN - nothing written. Re-run with --apply to write.")
        return 0

    print(f"writing title on {len(todo)} contacts...")
    try:
        written = write(hs, todo)
    except HubSpotMCPError as exc:
        print(f"\nwrite failed partway: {exc}")
        print("Re-run the script - contacts already written are skipped.")
        return 2
    print(f"\ndone. {written} contacts now have a title.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
