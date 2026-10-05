"""Configuration tab value rendering. Run: python -m pytest tests/test_config_display.py"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_panel import app  # noqa: E402,F401


def render(app, v):
    tpl = app.jinja_env.from_string('{% from "_macros.html" import value %}{{ value(v) }}')
    return tpl.render(v=v)


def test_lists_become_tags_not_python_repr(app):
    out = render(app, ["a", "b"])
    assert out.count('class="tag"') == 2 and "[" not in out


def test_bools_and_none(app):
    assert "true" in render(app, True) and "false" in render(app, False)
    assert ">-<" in render(app, None)


def test_nested_mapping_renders_as_sub_list(app):
    out = render(app, {"port": 22, "opts": ["x"]})
    assert 'class="props sub"' in out and "port" in out and "22" in out and "{" not in out


def test_text_is_escaped_and_long_values_get_a_title(app):
    assert "<script>" not in render(app, "<script>alert(1)</script>")
    assert 'title="' in render(app, "k" * 80)


def test_configuration_tab_renders(app):
    from test_panel import as_user
    r = as_user(app, "alice").get("/devices/pk-control?tab=configuration")
    assert r.status_code == 200 and "dl" in r.get_data(as_text=True)
