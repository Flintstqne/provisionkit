"""Pin script tests. Run: python -m pytest tests/test_pin_llm.py"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import pin_llm_artifacts as pin  # noqa: E402


@pytest.fixture()
def files(tmp_path):
    b, m = tmp_path / "llama-bin.zip", tmp_path / "tiny-q4.gguf"
    b.write_bytes(b"binary-archive")
    m.write_bytes(b"GGUF" + b"x" * 5000)
    return b, m


def test_prints_hashes(files, capsys):
    b, m = files
    pin.main(["--binary-url", b.as_uri(), "--model-url", m.as_uri()])
    out = capsys.readouterr().out
    assert f'llm_server_binary_sha256: "{hashlib.sha256(b.read_bytes()).hexdigest()}"' in out
    assert f'llm_server_model_sha256: "{hashlib.sha256(m.read_bytes()).hexdigest()}"' in out
    assert "llm_server_model_filename: tiny-q4.gguf" in out


def test_expected_hash_mismatch_stops(files):
    b, m = files
    with pytest.raises(SystemExit, match="does not match"):
        pin.main(["--binary-url", b.as_uri(), "--model-url", m.as_uri(), "--expect-model", "0" * 64])


def test_expected_hash_match_passes(files):
    b, m = files
    pin.main(["--binary-url", b.as_uri(), "--model-url", m.as_uri(),
              "--expect-binary", hashlib.sha256(b.read_bytes()).hexdigest().upper()])


def test_rejects_plain_http_and_non_gguf(files):
    b, m = files
    with pytest.raises(SystemExit, match="https"):
        pin.main(["--binary-url", "http://example.org/a.zip", "--model-url", m.as_uri()])
    with pytest.raises(SystemExit, match="gguf"):
        pin.main(["--binary-url", b.as_uri(), "--model-url", b.as_uri()])
