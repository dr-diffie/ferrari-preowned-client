"""Shared test fixtures: real captured responses, zero network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def dk_page0():
    return load("dk_page0.json")


@pytest.fixture(scope="session")
def us_enus():
    return load("us_enus_api.json")


@pytest.fixture(scope="session")
def us_dede():
    return load("us_dede_fresh.json")


@pytest.fixture(scope="session")
def be_current():
    return load("be_current.json")


@pytest.fixture(scope="session")
def thron_playlist():
    return load("thron_playlist_test.json")


@pytest.fixture(scope="session")
def dk_detail_html():
    return (FIXTURES / "dk_detail.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def oracle_pairs():
    return load("oracle_pairs.json")
