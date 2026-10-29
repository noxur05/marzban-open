#!/usr/bin/env python3
"""Generate key material for the Micky protocol (docs/micky-protocol.md).

    python3 scripts/micky_keys.py                # fresh production keys
    python3 scripts/micky_keys.py --id 2         # keys for rotation under a new key id
    python3 scripts/micky_keys.py --signing-only # just a signing key pair
    python3 scripts/micky_keys.py --vectors      # the cross-repo layer 2 test vectors (JSON)

Run it ON THE PANEL HOST so the secrets never travel: the panel .env gets the
link PUBLIC key and the signing SEED; the app build gets the link PRIVATE key
and the signing PUBLIC key. The signing seed must never reach the app or the
subscription relay.
"""

import argparse
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Load the module file directly: importing the `app` package would run
# app/__init__.py, which starts Xray. micky.py needs only config + cryptography.
_spec = importlib.util.spec_from_file_location("micky", os.path.join(ROOT, "app", "utils", "micky.py"))
micky = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(micky)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", type=int, default=1, help="key id (1-255) for these keys")
    parser.add_argument("--signing-only", action="store_true", help="only a signing key pair")
    parser.add_argument("--vectors", action="store_true", help="print the shared test vectors instead")
    args = parser.parse_args()

    if args.vectors:
        print(json.dumps(micky.build_test_vectors(), ensure_ascii=False, indent=2))
        return
    if not 1 <= args.id <= 255:
        parser.error("--id must be between 1 and 255")

    keys = micky.generate_keys()
    kid = args.id
    print("# --- Marzban .env (panel only) ---")
    if not args.signing_only:
        print(f'MICKY_LINK_PUBLIC_KEYS = "{kid}:{keys["link_public"]}"')
        print(f"MICKY_LINK_KEY_ID = {kid}")
    print(f'MICKY_SIGNING_KEYS = "{kid}:{keys["signing_seed"]}"')
    print(f"MICKY_SIGNING_KEY_ID = {kid}")
    print()
    print("# --- Micky app build (CI secrets) ---")
    if not args.signing_only:
        print(f'MICKY_LINK_PRIVATE_KEYS = "{kid}:{keys["link_private"]}"')
    print(f'MICKY_SIGNING_PUBLIC_KEYS = "{kid}:{keys["signing_public"]}"')
    print()
    print("# When rotating, APPEND to the existing lists, ship the app with both keys first,")
    print("# then switch the *_KEY_ID on the panel.")


if __name__ == "__main__":
    main()
