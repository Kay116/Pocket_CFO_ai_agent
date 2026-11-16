"""Check that the RUNTIME key can do everything Pocket CFO needs. Prints no secrets.

    # same PowerShell window where CLOUDANT_URL and CLOUDANT_RUNTIME_APIKEY are set
    python watsonx/db/check_cloudant.py

Each step prints OK or FAIL with the HTTP status, so you can tell whether a problem is the
API key (IAM), the role (Writer), the database name, or the network.
"""
import os
import sys

from ibm_cloud_sdk_core import ApiException
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from ibmcloudant.cloudant_v1 import CloudantV1, Document

DB, AUDIT_DB = "pocket_cfo", "pocket_cfo_audit"
DOC_ID = "diagnostic:check"


def step(name, fn):
    try:
        out = fn()
        print(f"OK    {name}")
        return out
    except ApiException as exc:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", "?")
        print(f"FAIL  {name}: HTTP {status} - {exc.message}")
    except Exception as exc:  # network, SSL, import...
        print(f"FAIL  {name}: {type(exc).__name__}")
    return None


def main() -> int:
    url, key = os.environ.get("CLOUDANT_URL"), os.environ.get("CLOUDANT_RUNTIME_APIKEY")
    if not url or not key:
        print("Set CLOUDANT_URL and CLOUDANT_RUNTIME_APIKEY in this window first.")
        return 1
    print(f"URL: {url}   key length: {len(key.strip())}")
    auth = IAMAuthenticator(key.strip())
    if step("1. IAM: exchange API key for a token", lambda: auth.token_manager.get_token()) is None:
        print("   -> The runtime key is wrong or deleted. Create a new key on the pocket-cfo-runtime service ID.")
        return 2
    client = CloudantV1(authenticator=auth)
    client.set_service_url(url.strip().rstrip("/"))

    info = step("2. Read database info (pocket_cfo)", lambda: client.get_database_information(db=DB).get_result())
    if info is not None:
        print(f"      partitioned={info.get('props', {}).get('partitioned', False)}")
    existing = None
    try:
        existing = client.get_document(db=DB, doc_id=DOC_ID).get_result()
    except ApiException:
        pass
    body = {"_id": DOC_ID, "type": "diagnostic"}
    if existing:
        body["_rev"] = existing["_rev"]
    w = step("3. Write a test document", lambda: client.put_document(db=DB, doc_id=DOC_ID,
                                                                       document=Document.from_dict(body)).get_result())
    step("4. Read it back", lambda: client.get_document(db=DB, doc_id=DOC_ID).get_result())
    step("5. List the partition", lambda: client.post_partition_all_docs(db=DB, partition_key="diagnostic").get_result())
    if w:
        step("6. Delete the test document", lambda: client.delete_document(db=DB, doc_id=DOC_ID, rev=w["rev"]).get_result())
    step("7. Write to the audit database", lambda: client.post_document(
        db=AUDIT_DB, document=Document.from_dict({"user_key": "diagnostic", "event": "check"})).get_result())
    print("\nIf 2-7 fail with HTTP 401/403: give the pocket-cfo-runtime service ID the Writer role on this Cloudant instance,")
    print("and make sure the instance allows IAM authentication (Manage -> Authentication method).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
