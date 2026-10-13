import os
import sys

from app import DATABASE_FILE, provision_super_admin


def main():
    password = os.environ.get("SHIPPINGFLOW_SUPER_ADMIN_PASSWORD")
    if password is None:
        raise ValueError("Set SHIPPINGFLOW_SUPER_ADMIN_PASSWORD before running this script.")
    user = provision_super_admin("waqasyousafy", password)
    action = "Promoted" if user.get("promoted") else "Created"
    print(
        f"{action} super-admin account '{user['username']}' "
        f"in {DATABASE_FILE}."
    )


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"Super-admin provisioning failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
