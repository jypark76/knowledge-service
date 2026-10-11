# In plain English: tests for the check that the AI model files packed into the image are exactly the
# files we pinned. The model is downloaded while the image is built. Without a check, a changed or
# tampered file on the download site would be packed into the image silently (OWASP A05 CWE-114 and
# the supply-chain risks in A03 and A08).
#
# The real check is two lines in the Dockerfile that run when the image is built: they compare every
# model file with the hashes in model/model.sha256 and FAIL the build on any difference. The pipeline's
# image build job runs it on every pull request. These tests make sure that check, and the pin file it
# uses, cannot be quietly removed or damaged.
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
PIN = ROOT / "model" / "model.sha256"

# The five files the model needs. If fastembed ever needs another file, it must be pinned too.
EXPECTED_FILES = {"config.json", "model_optimized.onnx", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json"}


def pin_lines():
    return [line for line in PIN.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.owasp("A05", 114)
def test_the_pin_file_lists_every_model_file_with_a_sha256_hash():
    lines = pin_lines()
    found = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
        assert match, f"not a 'hash  filename' line: {line!r}"
        found[match.group(2)] = match.group(1)
    assert set(found) == EXPECTED_FILES
    assert len(set(found.values())) == len(found), "two files share one hash, which is almost certainly a mistake"


@pytest.mark.owasp("A05", 114)
def test_the_dockerfile_checks_the_model_files_against_the_pin_before_the_code_is_copied_in():
    download = DOCKERFILE.index("TextEmbedding('BAAI/bge-small-en-v1.5')")
    copy_pin = DOCKERFILE.index("COPY --chown=appuser model/model.sha256")
    check = DOCKERFILE.index("sha256sum -c /tmp/model.sha256")
    copy_code = DOCKERFILE.index("COPY --chown=appuser app/")
    assert download < copy_pin < check < copy_code


@pytest.mark.owasp("A05", 114)
def test_the_check_runs_inside_the_exact_model_version_folder():
    # A new model version lands in a different folder, so the build fails until the pin is updated on purpose.
    assert re.search(r"snapshots/[0-9a-f]{40}", DOCKERFILE)
    assert "cd /app/model_cache/" in DOCKERFILE


@pytest.mark.owasp("A05", 114)
def test_the_pin_file_is_not_left_out_of_the_image_build():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "model" not in [line.strip().rstrip("/") for line in ignored]


@pytest.mark.owasp("A05", 114)
def test_the_model_name_in_the_code_matches_the_name_in_the_dockerfile():
    code = (ROOT / "app" / "embeddings.py").read_text(encoding="utf-8")
    assert 'MODEL_NAME = "BAAI/bge-small-en-v1.5"' in code
    assert "TextEmbedding('BAAI/bge-small-en-v1.5')" in DOCKERFILE
