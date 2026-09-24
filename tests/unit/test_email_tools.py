"""The pieces around the model: the skill catalogue, the file reader, the footer and
the HTML part. Nothing here calls a model or a network."""

import json
from pathlib import Path

import pytest

from bench_outreach.email_agent.agent import (LeadTools, as_html, check_skill_files,
                                              load_system_prompt, render_system_prompt,
                                              skill_catalogue, with_footer)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED = REPO_ROOT / "skills"
SHIPPED_PROMPT = REPO_ROOT / "config" / "EMAIL_AGENT.md"
SENDER = {"name": "Priya Nair", "company": "Tek Ninjas",
          "postal_address": "1 Example Way, Charlotte NC 28202"}
BODY = "Srinivas,\n\nWe have a role open.\n\nBest,\nStephen Miller"


def write_agent_md(root: Path, body: str = "", frontmatter: str | None = None) -> Path:
    """A minimal EMAIL_AGENT.md: frontmatter then body, parsed exactly like a SKILL.md."""
    target = root / "EMAIL_AGENT.md"
    head = frontmatter if frontmatter is not None else (
        "name: email-agent\ndescription: notes for whoever edits this file\n")
    target.write_text(f"---\n{head}---\n\n"
                      + (body or "You are the Email Agent.\n\n{{SKILLS}}\n\n"
                                 "Sign as: {{SENDER_NAME}}.\n"
                                 "Never state a client, rate, location, duration or contract type."),
                      encoding="utf-8")
    return target


def write_skill(root: Path, name: str, description: str = "Does a thing. Use when asked.") -> Path:
    d = root / name
    (d / "assets").mkdir(parents=True)
    (d / "references").mkdir()
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
                                encoding="utf-8")
    return d


# ------------------------------------------------------------ the catalogue
def test_catalogue_is_names_and_descriptions_only(tmp_path):
    write_skill(tmp_path, "alpha", "First skill. Use when A.")
    write_skill(tmp_path, "beta", "Second skill. Use when B.")
    cat = skill_catalogue(tmp_path)
    assert [(s.name, s.description) for s in cat] == [("alpha", "First skill. Use when A."),
                                                      ("beta", "Second skill. Use when B.")]
    assert all(len(s.sha) == 12 for s in cat)


def test_a_skill_without_a_description_cannot_be_offered(tmp_path):
    (tmp_path / "mute").mkdir()
    (tmp_path / "mute" / "SKILL.md").write_text("---\nname: mute\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="no description"):
        skill_catalogue(tmp_path)


def test_an_empty_skills_folder_is_a_startup_error(tmp_path):
    with pytest.raises(ValueError, match="no skills"):
        skill_catalogue(tmp_path)


def test_the_system_prompt_lists_every_skill_and_where_to_read_it(tmp_path):
    md = write_agent_md(tmp_path)
    write_skill(tmp_path, "alpha", "First skill.")
    text = render_system_prompt(load_system_prompt(md), skill_catalogue(tmp_path), SENDER)
    assert "**alpha** — First skill." in text and "`alpha/SKILL.md`" in text
    assert "Sign as: Priya Nair" in text
    for withheld in ("client", "rate", "location", "duration", "contract type"):
        assert withheld in text, "the hard rules live in the system prompt, not only the skill"


# --------------------------------------------------- the system prompt's file
def test_the_frontmatter_is_not_sent_to_the_model(tmp_path):
    """The file documents itself in frontmatter. That is for people, not the model."""
    md = write_agent_md(tmp_path)
    write_skill(tmp_path, "alpha")
    text = render_system_prompt(load_system_prompt(md), skill_catalogue(tmp_path), SENDER)
    assert "notes for whoever edits" not in text
    assert text.startswith("You are the Email Agent.")


def test_frontmatter_is_required(tmp_path):
    """Parsed like a SKILL.md, so it is held to the same shape."""
    (tmp_path / "EMAIL_AGENT.md").write_text("bare {{SKILLS}} {{SENDER_NAME}}", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        load_system_prompt(tmp_path / "EMAIL_AGENT.md")


def test_a_missing_file_stops_the_service_at_startup(tmp_path):
    with pytest.raises(ValueError, match="EMAIL_AGENT.md"):
        load_system_prompt(tmp_path / "EMAIL_AGENT.md")


def test_a_missing_placeholder_is_a_startup_error(tmp_path):
    """Without {{SKILLS}} the model is never told which skills exist — that would
    fail per lead, silently, rather than at boot."""
    md = write_agent_md(tmp_path, "You are the Email Agent. Sign as: {{SENDER_NAME}}.")
    with pytest.raises(ValueError, match="SKILLS"):
        load_system_prompt(md)


def test_braces_in_the_file_survive(tmp_path):
    """Substitution is a plain replace, so a JSON example in the prose is safe."""
    md = write_agent_md(tmp_path, 'Reply with {"subject": "..."}\n{{SKILLS}}\n{{SENDER_NAME}}')
    write_skill(tmp_path, "alpha")
    text = render_system_prompt(load_system_prompt(md), skill_catalogue(tmp_path), SENDER)
    assert '{"subject": "..."}' in text


def test_the_shipped_system_prompt_loads_and_is_fingerprinted():
    got = load_system_prompt(SHIPPED_PROMPT)
    assert got.path.name == "EMAIL_AGENT.md" and len(got.sha) == 12
    assert "{{SKILLS}}" in got.text, "placeholders are filled at send time, not on disk"


def test_the_system_prompt_is_not_inside_the_skills_folder():
    """agentskills.io/specification: a skill is a DIRECTORY holding a SKILL.md. The
    system prompt is what the model is told before it picks one, so it must not sit
    among them — skill_catalogue() would end up offering the agent its own prompt."""
    assert not list(SHIPPED.glob("*.md")), "skills/ holds skill directories only"
    assert SHIPPED_PROMPT.exists()


def test_the_shipped_skills_folder_is_consistent():
    """Every asset names a reference that exists — the check /readyz runs."""
    assert check_skill_files(SHIPPED) == []
    assert [s.name for s in skill_catalogue(SHIPPED)] == ["outreach"]


def test_a_dangling_reference_is_caught_at_startup(tmp_path):
    d = write_skill(tmp_path, "alpha")
    (d / "assets" / "java.json").write_text(json.dumps({"technology": "Java", "reference": "ghost.md"}),
                                            encoding="utf-8")
    problems = check_skill_files(tmp_path)
    assert len(problems) == 1 and "ghost.md" in problems[0]


# ------------------------------------------------------------ the file reader
def _tools(root: Path) -> LeadTools:
    return LeadTools({"email": "x@example.com"}, "1", "trg", sender=None, identity=SENDER,
                     skills_root=root, divert="", dry_run=True)


def test_reads_only_inside_the_skills_folder(tmp_path):
    d = write_skill(tmp_path, "alpha")
    (d / "references" / "r.md").write_text("ref body", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("nope", encoding="utf-8")
    tools = _tools(tmp_path)
    assert tools.read_skill_file("alpha/references/r.md") == "ref body"
    assert tools.read_skill_file("../secret.txt").startswith("Error")
    assert tools.read_skill_file("alpha/../../secret.txt").startswith("Error")
    assert tools.read_skill_file("alpha/references").startswith("Error"), "a folder is not a file"
    assert tools.files_read == ["alpha/references/r.md"]


# --------------------------------------------------------------- the footer
def test_footer_is_added_by_code_with_address_and_unsubscribe():
    out = with_footer("Hi there.\n\nBest,\nPriya", SENDER, "%unsubscribe_url%")
    assert "1 Example Way, Charlotte NC 28202" in out
    assert "Unsubscribe: %unsubscribe_url%" in out
    assert out.startswith("Hi there.") and "<p>" not in out


# -------------------------------------------------------------- the HTML part
def test_unsubscribe_is_a_word_not_a_url():
    out = as_html(BODY, SENDER, "%unsubscribe_url%")
    assert '<a href="%unsubscribe_url%">Unsubscribe</a>' in out
    assert "Unsubscribe: %unsubscribe_url%" not in out


def test_the_postal_address_survives_into_the_html():
    out = as_html(BODY, SENDER, "%unsubscribe_url%")
    assert "1 Example Way, Charlotte NC 28202" in out and "Tek Ninjas" in out


def test_blank_lines_become_paragraphs_and_single_breaks_become_br():
    out = as_html(BODY, SENDER, "%unsubscribe_url%")
    assert out.startswith("<p>Srinivas,</p>")
    assert "<p>Best,<br>Stephen Miller</p>" in out


def test_model_output_is_escaped_never_rendered_as_markup():
    hostile = 'Hi <script>alert(1)</script> & "co" rates < 5%'
    out = as_html(hostile, SENDER, "%unsubscribe_url%")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out and "&amp;" in out and "&quot;" in out


def test_html_carries_no_marketing_furniture():
    out = as_html(BODY, SENDER, "%unsubscribe_url%").lower()
    for junk in ("<table", "<img", "<center", "font-family", "background-color", "<div"):
        assert junk not in out, f"the html part grew a {junk!r}"
