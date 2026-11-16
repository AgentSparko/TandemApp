"""keyword-router: a TandemApp decision provider built on tandemapp_plugin (SRV-861).

It answers the conductor's classify and judge questions: the first rule of the question whose
word the text holds names the label (when the question offers it); else the first option, with
confidence 0. The text arrives redacted (prompts:redacted). With settings:read granted it reads
its rules from the host at each question (tandem/settings), else from its start.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tandemapp_plugin import Plugin  # noqa: E402

plugin = Plugin()


def current() -> dict:
    return plugin.read_settings() if plugin.granted("settings:read") else plugin.settings


def answer(ask) -> dict:
    s = current()
    text = (ask.text or "").lower()
    for rule in s.get("rules") or []:
        if rule.get("question") != ask.question or rule.get("label") not in ask.options:
            continue
        if any(w.lower() in text for w in rule.get("words") or []):
            return {"label": rule["label"], "confidence": float(s.get("confidence", 0.7))}
    return {"label": next(iter(ask.options)), "confidence": 0.0}


@plugin.decision("rules", question="classify")
def classify(ask, ctx):
    return answer(ask)


@plugin.decision("rules", question="judge")
def judge(ask, ctx):
    return answer(ask)


if __name__ == "__main__":
    plugin.main()
