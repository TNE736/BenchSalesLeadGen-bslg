# Python

Background on the Python ecosystem itself. Every Python lead gets this, whatever their
title — the role-specific material comes from `assets/python.json`, not from here.

Python is a language, not a product, and that changes how these emails have to work.
A Salesforce Architect and a Salesforce Administrator work on the same platform. A
Python backend engineer and an ML engineer share syntax and almost nothing else —
different tools, different problems, different conferences. The only thing that makes
an email credible here is naming the specific stack the person actually works in.

## How the work divides

**Backend and API** — HTTP services, usually containerised. The framework question
moved recently: FastAPI now leads Django and Flask, and leading with "Django/Flask"
reads as a req written in 2019.

**Data engineering** — pipelines into a warehouse or lakehouse. Orchestration,
transformation, modelling, and the tests that stop bad data reaching a dashboard.
Much of the contract market is migration work off older estates.

**Machine learning** — training, serving and monitoring models as production
services. Distinct from data science, where the deliverable is an analysis.

**GenAI** — applications built on top of foundation models. This barely existed as a
staffing category two years ago and is now the most contract-heavy Python niche.

**Infrastructure and automation** — Python as the glue around Terraform, Kubernetes
and delivery pipelines. These people are infrastructure engineers first.

**Test automation** — frameworks and suites that gate releases, not manual testing.

## Vocabulary to get right

These are the errors that make a Python consultant stop reading.

**Do not call a data scientist an ML engineer, or the reverse.** One ships a running
service; the other produces an analysis. Conflating them reads as not understanding
the difference.

**Do not call RAG work "training a model".** Retrieval-augmented generation involves
no training at all. This is the fastest way to lose credibility with a GenAI engineer.
For the same reason, almost nobody "builds an LLM" — they build on one.

**LangGraph, not LangChain.** LangChain is increasingly the thing teams migrated away
from. And "prompt engineer" has been largely discredited as a job title — it is a
skill inside AI engineering, not a role.

**Selenium is the legacy default; Playwright is the modern one.** But Playwright's
centre of gravity is TypeScript, so a Python QA person is not automatically a
Playwright person — check before assuming. pytest is the safe constant in this family.

**"Manual tester" or "QA tester" is an insult to an SDET.** They write frameworks.

**"ETL developer" signals Informatica-era work.** Practitioners say ELT, or just
pipelines. Likewise "Big Data", "Hadoop" and "Hive" are migration-away signals in
2026, not modern ones.

**Airflow renamed Datasets to Assets in 3.0.** Saying Datasets dates you.

**TensorFlow for new build is dated** — PyTorch is the default, and a TensorFlow req
usually means an existing model estate.

**Do not pitch an infrastructure engineer as a "Python developer".** They will correct
you. And "Python scripting" undersells what a senior platform engineer does, even
though reqs phrase it that way.

**Python 2 to 3 migration** is long dead as a category. Python 3.12 is the modal
version in use, with 3.11 and 3.13 either side of it.
