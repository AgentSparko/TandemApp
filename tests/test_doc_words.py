"""TandemApp's texts keep two rules the engine's texts keep (OPS-149; ENG-413 WP14: TandemApp's half
of the engine's tests/test_install_docs.py, which reads only the engine's texts now).

  1  no doc, contract README or page source counts the models ("five models", "three models"): the
     set changes with every release                          test_no_model_count
  2  nothing under contracts/ calls the Setup Guide's figure a bird (the page and docs/ are held to
     the same words by dashboard/tests/setup-wren.test.ts)   test_no_doc_calls_the_figure_a_bird

Run: python tests/test_doc_words.py
"""

from __future__ import annotations

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# a count of the models: "five models", "3 models", "all five share"; not "the Qwen3.8 models" or a
# step list's "0 models"
COUNT = re.compile(r"(?<![\w.])(?:two|three|four|five|six|seven|eight|nine|ten|[2-9]|1[0-9])\s+"
                   r"(?:big |base |large |supported |different )?models\b|\ball (?:three|four|five|six|seven) share\b", re.I)
# the intro's "two models working in tandem" is the drafter and the model, not the set
ALLOWED = ("two models working in tandem",)
COUNT_GLOBS = (("docs", "**", "*.md"), ("contracts", "**", "*.md"), ("dashboard", "src", "**", "*.ts"))
BIRD = re.compile(r"\b(bird|beak|feathers?|wings?|perch\w*|chirp\w*|preen\w*|ruffle\w*)\b", re.I)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_no_model_count():
    files = []
    for parts in COUNT_GLOBS:
        found = glob.glob(os.path.join(ROOT, *parts), recursive=True)
        assert found, f"{'/'.join(parts)} matches no file: the check would read nothing there"
        files += found
    hits = []
    for f in files:
        text = _read(f)
        for m in COUNT.finditer(text):
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
            if not any(a in line for a in ALLOWED):
                hits.append(f"{os.path.relpath(f, ROOT)}: {m.group(0)!r}")
    assert not hits, hits
    return f"{len(files)} files, no count of the models"


def test_no_doc_calls_the_figure_a_bird():
    files = sorted(os.path.join(d, f) for d, _, fs in os.walk(os.path.join(ROOT, "contracts")) for f in fs
                   if re.search(r"\.(ts|css|md|html|json)$", f))
    assert files, "contracts/ holds no file the check reads"
    hits = [f"{os.path.relpath(f, ROOT)}: {m.group(0)!r}" for f in files for m in BIRD.finditer(_read(f))]
    assert not hits, hits
    return f"{len(files)} contract files"


if __name__ == "__main__":
    from scriptmode import run
    run(globals())
