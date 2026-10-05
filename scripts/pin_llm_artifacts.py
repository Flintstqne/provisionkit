#!/usr/bin/env python3
"""Download the llama.cpp archive and the model once, hash them, and print inventory variables.

Run it on a machine with internet access, then review the printed values before putting them in
inventories/local/group_vars/llm_servers.yml. If the publisher lists a SHA-256, pass it with --expect-binary /
--expect-model so a mismatch stops you.

  scripts/pin_llm_artifacts.py --binary-url URL --model-url URL [--expect-binary HEX] [--expect-model HEX]
"""
import argparse
import hashlib
import sys
import urllib.request
from urllib.parse import urlsplit

CHUNK = 1 << 20


def sha256_of(url):
    """Stream the URL through SHA-256. Only https and file URLs are accepted."""
    if urlsplit(url).scheme not in ("https", "file"):
        raise SystemExit(f"Refusing {url}: use an https URL.")
    h, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (scheme checked above)
        while chunk := r.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
            print(f"\r  {size / 1e6:,.0f} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    return h.hexdigest(), size


def pin(name, url, expected):
    print(f"Hashing {name}: {url}", file=sys.stderr)
    digest, size = sha256_of(url)
    if expected and expected.lower() != digest:
        raise SystemExit(f"{name}: SHA-256 {digest} does not match the expected {expected.lower()}.")
    if size == 0:
        raise SystemExit(f"{name}: empty download.")
    return digest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--binary-url", required=True)
    p.add_argument("--model-url", required=True)
    p.add_argument("--expect-binary")
    p.add_argument("--expect-model")
    a = p.parse_args(argv)
    filename = urlsplit(a.model_url).path.rsplit("/", 1)[-1]
    if not filename.endswith(".gguf"):
        raise SystemExit("The model URL must point to a .gguf file.")
    binary = pin("binary", a.binary_url, a.expect_binary)
    model = pin("model", a.model_url, a.expect_model)
    print(f'llm_server_binary_url: "{a.binary_url}"')
    print(f'llm_server_binary_sha256: "{binary}"')  # quoted: YAML reads digit-only hex as a number
    print(f'llm_server_model_url: "{a.model_url}"')
    print(f'llm_server_model_sha256: "{model}"')
    print(f"llm_server_model_filename: {filename}")


if __name__ == "__main__":
    main()
