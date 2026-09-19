"""The shipped skill, as the model will read it. These are content checks on the
files themselves — the code no longer parses them, the model does."""

import json
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "outreach"
SKILL_MD = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
ASSET = json.loads((SKILL_DIR / "assets" / "salesforce.json").read_text(encoding="utf-8"))
REFERENCE = (SKILL_DIR / "references" / "salesforce.md").read_text(encoding="utf-8")

HUBSPOT_TITLES = {
    "Salesforce Developer", "Salesforce Lead Developer", "Salesforce Architect",
    "Salesforce Administrator", "Salesforce Cloud", "Salesforce Consultant",
    "Salesforce Business Analyst", "Salesforce QA",
}


def test_skill_md_says_when_to_use_it():
    front = re.match(r"\A---\s*\n(.*?)\n---", SKILL_MD, re.DOTALL).group(1)
    assert "name: outreach" in front
    assert "Use when" in front, "the description is what the model reads to decide"


def test_skill_md_tells_the_model_where_the_material_is():
    for pointer in ("assets/<technology>.json", "references/<technology>.md", "flag_lead", "send_email"):
        assert pointer in SKILL_MD, f"SKILL.md no longer mentions {pointer!r}"


def test_skill_md_still_withholds_the_specifics():
    """The model may assert a requirement exists. It may not invent its details."""
    lowered = SKILL_MD.lower()
    for withheld in ("client", "rate", "location", "duration", "contract type"):
        assert withheld in lowered, f"SKILL.md no longer withholds {withheld!r}"
    assert "Invent nothing" in SKILL_MD and "Under 300 words" in SKILL_MD


def test_asset_covers_all_eight_hubspot_titles():
    """Every title in HubSpot's dropdown must have an entry, or those leads stall."""
    assert set(ASSET["titles"]) == HUBSPOT_TITLES
    assert ASSET["technology"] == "Salesforce" and ASSET["reference"] == "salesforce.md"


def test_each_title_has_its_own_description_and_involves():
    descriptions = [e["description"] for e in ASSET["titles"].values()]
    assert len(set(descriptions)) == len(descriptions), "two titles share a description"
    for title, entry in ASSET["titles"].items():
        assert len(entry["involves"]) >= 5, f"{title} has too few entries to be specific"
        assert "skills" not in entry, f"{title} still uses the overloaded word"


def test_reference_is_about_the_platform_not_one_title():
    """Read for all eight titles, so it must not be developer-framed."""
    first_heading = next(l for l in REFERENCE.splitlines() if l.startswith("# "))
    assert first_heading.strip() == "# Salesforce", first_heading
    lowered = REFERENCE.lower()
    assert "75% coverage" not in lowered
    assert "separates a developer who has shipped" not in lowered


def test_reference_offers_no_job_the_model_could_pitch():
    lowered = REFERENCE.lower()
    for leak in ("open roles", "w2 only", "12-month contract", "onsite:", "charlotte"):
        assert leak not in lowered, f"the reference file still contains {leak!r}"
