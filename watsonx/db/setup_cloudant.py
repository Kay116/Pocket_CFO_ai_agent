"""One-time Cloudant setup for Pocket CFO. Run locally, never commit credentials.

    export CLOUDANT_URL="https://<account>.cloudantnosqldb.appdomain.cloud"
    export CLOUDANT_APIKEY="<IAM key with Manager role, used only for setup>"
    python watsonx/db/setup_cloudant.py

Creates:
  pocket_cfo        PARTITIONED database (partition key = HMAC user_key) for profiles + snapshots
  pocket_cfo_audit  non-partitioned database for audit events (user_key + event name only)

Afterwards, give the runtime service ID only the Writer role (not Manager) and put
its key in the watsonx Orchestrate connection `pocket_cfo_cloudant`.
"""
import os
import sys

from ibm_cloud_sdk_core import ApiException
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibmcloudant.cloudant_v1 import CloudantV1

DATABASES = [("pocket_cfo", True), ("pocket_cfo_audit", False)]


def main() -> int:
    url, key = os.environ.get("CLOUDANT_URL"), os.environ.get("CLOUDANT_APIKEY")
    if not url or not key:
        print("Set CLOUDANT_URL and CLOUDANT_APIKEY first.", file=sys.stderr)
        return 1
    client = CloudantV1(authenticator=IAMAuthenticator(key))
    client.set_service_url(url)
    for name, partitioned in DATABASES:
        try:
            client.put_database(db=name, partitioned=partitioned).get_result()
            print(f"created {name} (partitioned={partitioned})")
        except ApiException as exc:
            if getattr(exc, "status_code", None) == 412:
                info = client.get_database_information(db=name).get_result()
                is_part = info.get("props", {}).get("partitioned", False)
                if is_part != partitioned:
                    print(f"ERROR: {name} exists but partitioned={is_part}; expected {partitioned}. "
                          f"Delete it or choose another CLOUDANT_DB name.", file=sys.stderr)
                    return 2
                print(f"{name} already exists (partitioned={is_part}) - OK")
            else:
                print(f"ERROR creating {name}: HTTP {getattr(exc, 'status_code', '?')}", file=sys.stderr)
                return 3
    print("Cloudant is ready for Pocket CFO.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
