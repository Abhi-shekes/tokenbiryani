"""`tokenbiryani-oauth-check` — see what your credentials file actually contains.

This package cannot verify the CLI's credentials schema, so rather than guess and
fail mysteriously, it ships a command that shows you the shape of your own file and
whether the configured paths resolve. Tokens are never printed in full.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional

from tokenbiryani.providers.oauth_credentials import (
    DEFAULT_EXPIRY_PATH,
    DEFAULT_PATH,
    DEFAULT_TOKEN_PATH,
    CredentialError,
    CredentialsFile,
    key_shape,
    redact,
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--token-path", default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--expiry-path", default=DEFAULT_EXPIRY_PATH)
    args = parser.parse_args(argv)

    source = CredentialsFile(args.path, args.token_path, args.expiry_path)
    print()
    print(f"  file        {source.path}")

    try:
        data = source.load()
    except CredentialError as exc:
        print(f"  status      unreadable — {exc}")
        print()
        return 1

    print("  keys        " + ("\n              ".join(key_shape(data)) or "(none)"))

    expiry = source.expires_at()
    if expiry is None:
        print(f"  expiry      not found at {args.expiry_path!r}")
    else:
        remaining = expiry - time.time()
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry))
        print(f"  expiry      {when} ({remaining / 3600:.1f}h from now)")

    try:
        token = source.token()
    except CredentialError as exc:
        print(f"  token       unusable — {exc}")
        print()
        return 1

    print(f"  token       {redact(token)}  (at {args.token_path!r})")
    print()
    print("  Looks usable. Configure the account as:")
    print()
    print("    accounts:")
    print("      - id: acct-oauth")
    print("        type: oauth")
    print("        observable_limits: false")
    if args.path != DEFAULT_PATH or args.token_path != DEFAULT_TOKEN_PATH:
        print("        options:")
        print(f"          credentials_path: {args.path}")
        print(f"          token_path: {args.token_path}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
