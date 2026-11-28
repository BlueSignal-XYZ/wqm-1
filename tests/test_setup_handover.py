"""The setup page's handover from BlueSignal Setup to the home Wi-Fi.

Jacques, 2026-11-17 (Pi 4, card 50c7151): "the device page doesn't know
where to go so then it keeps suggesting that the device is being connected
to when it's not … There should be a clear and seamless handover to the next
wifi." The page answered its own Connect with "Already connecting to … —
watch the lights", froze on that line once the hotspot went away, and no
light shows a join (install plan §7 is not built).

The page's own script is run under node (``tests/setup_page_handover.js``)
against a fake page and a scripted unit, in English and in Spanish.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 - node runs this repo's page script
from html.parser import HTMLParser
from pathlib import Path

import pytest

from utils import captive_portal, netctl, setup_copy

HARNESS = Path(__file__).resolve().parent / "setup_page_handover.js"


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline = ""
        self.copy = ""
        self.hidden_ids: list[str] = []
        self._into: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a and "hidden" in a:
            self.hidden_ids.append(a["id"])
        if tag == "script":
            self._into = "copy" if a.get("id") == "copy" else "inline"

    def handle_endtag(self, tag):
        if tag == "script":
            self._into = None

    def handle_data(self, data):
        if self._into == "copy":
            self.copy += data
        elif self._into == "inline":
            self.inline += data


def _page(lang: str) -> _Scripts:
    parsed = _Scripts()
    parsed.feed(captive_portal.render_page(lang).decode("utf-8"))
    return parsed


@pytest.mark.parametrize("lang", ["en", "es"])
def test_the_page_walks_the_handover(lang, tmp_path):
    node = shutil.which("node")
    assert node, "node is required to run the setup page's script"
    parsed = _page(lang)
    payload = tmp_path / "page.json"
    payload.write_text(
        json.dumps(
            {
                "copy": json.loads(parsed.copy),
                "script": parsed.inline,
                "hidden_ids": parsed.hidden_ids,
                "poll_timeout_ms": captive_portal.POLL_TIMEOUT_MS,
                "give_up_ms": captive_portal.HANDOVER_GIVE_UP_S * 1000,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(  # nosec B603 - fixed argv, this repo's harness
        [node, str(HARNESS), str(payload)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize("lang", ["en", "es"])
def test_the_panel_says_what_happens_next(lang):
    page = captive_portal.render_page(lang).decode("utf-8")
    c = setup_copy.LANGUAGES[lang]
    for key in ("handover_off", "handover_back", "handover_joined"):
        assert c[key] in page or c[key].replace("'", "&#x27;") in page, key
    assert 'id="handover" hidden' in page


def test_no_sentence_sends_anyone_to_the_lights():
    """No light shows a join until install plan §7 is built."""
    for lang in setup_copy.LANGUAGES.values():
        for key, sentence in lang.items():
            low = sentence.lower()
            assert "light" not in low and "luces" not in low, key


def test_the_page_waits_out_the_longest_failed_join():
    """A join that fails at the end of BlueSignal's proof window gives the
    hotspot back last. The page must not say "most likely joined" first."""
    longest = netctl.JOIN_BUDGET_S + netctl.BLUESIGNAL_HOLD_S + 45
    assert longest <= captive_portal.HANDOVER_GIVE_UP_S
    assert captive_portal.HANDOVER_GIVE_UP_S <= 6 * 60


def test_the_five_minutes_in_the_copy_matches_the_page():
    minutes = captive_portal.HANDOVER_GIVE_UP_S / 60
    assert minutes == 5
    assert "five minutes" in setup_copy.en["handover_back"]
    assert "cinco minutos" in setup_copy.es["handover_back"]


def test_a_poll_gives_up_before_the_next_one_is_due():
    assert 0 < captive_portal.POLL_TIMEOUT_MS < 2 * captive_portal.POLL_MS
