"""translation_pipeline.py reads translator.py as `core`; those names must stay there.

Several of them are imports translator.py never uses itself (they exist for the pipeline
and for tests that patch them on translator), so an unused-import cleanup would delete
them and the pipeline would only fail at run time. This test fails first.
"""
import re
from pathlib import Path

import modules.translation_pipeline as pipeline
import modules.translator as translator

ROOT = Path(__file__).resolve().parents[1]


def test_every_core_name_the_pipeline_reads_exists_on_translator():
    source = (ROOT / "modules" / "translation_pipeline.py").read_text(encoding="utf-8")
    names = set(re.findall(r"\bcore\.(\w+)", source))
    assert names, "the pipeline no longer reads translator as core"
    missing = sorted(name for name in names if name not in vars(translator))
    assert not missing, f"translator.py lost names the pipeline reads as core.X: {missing}"


def test_translator_still_answers_the_moved_names():
    for name in translator._PIPELINE_NAMES:
        assert getattr(translator, name) is getattr(pipeline, name)
    assert translator.start is pipeline.start
