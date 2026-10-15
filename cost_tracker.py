"""
Real GCP billing reconciliation for the Nexus cost dashboard.

server.py's GET /costs is a live, instant estimate built from token counts —
accurate to within the margin documented in config.py, but not Google's own
billed figure. This module reads the actual figure from the BigQuery billing
export, once the user links it in Cloud Billing Console (console-only step,
no API/gcloud command exists for that specific link as of this writing).

Degrades gracefully to "not yet available" — Google's billing export has no
real-time guarantee and does not backfill, so for some time after setup this
will correctly report nothing rather than guess.
"""
import datetime
import os
from pathlib import Path

GCP_PROJECT = "oodlestack"
GCP_DATASET = "nexus_billing_export"
BILLING_ACCOUNT_ID = "016B74-639C89-6DB8FF"
SERVICE_ACCOUNT_KEY = str(Path.home() / ".gcp" / "nexus-billing-reader.json")


def _table_name() -> str:
    # Standard Cloud Billing export table naming convention:
    # gcp_billing_export_v1_<billing_account_id, dashes -> underscores>
    return f"gcp_billing_export_v1_{BILLING_ACCOUNT_ID.replace('-', '_')}"


def query_real_gemini_cost(days: int = 30) -> dict | None:
    """
    Returns {"total", "currency", "by_service", "last_updated", "window_days"}
    or None if the export isn't available yet (not linked, first daily cycle
    hasn't landed, or anything else goes wrong — never raises).
    """
    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError:
        return None

    if not os.path.exists(SERVICE_ACCOUNT_KEY):
        return None

    try:
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_KEY)
        client = bigquery.Client(project=GCP_PROJECT, credentials=creds)
        table = f"{GCP_PROJECT}.{GCP_DATASET}.{_table_name()}"

        query = f"""
            SELECT
                service.description AS service,
                SUM(cost) AS total_cost,
                ANY_VALUE(currency) AS currency
            FROM `{table}`
            WHERE usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
            GROUP BY service
            ORDER BY total_cost DESC
        """
        rows = list(client.query(query).result())
    except Exception as e:
        # Table not found (export not linked yet, or no data in the first
        # cycle), auth issue, API not enabled, etc. — all equally mean
        # "not available right now", never a hard failure.
        print(f"[COST] Real billing query unavailable: {e}", flush=True)
        return None

    by_service = {r.service: round(r.total_cost, 4) for r in rows}
    total = round(sum(r.total_cost for r in rows), 4)
    currency = rows[0].currency if rows else "USD"

    return {
        "total": total,
        "currency": currency,
        "by_service": by_service,
        "last_updated": datetime.datetime.now().isoformat(),
        "window_days": days,
    }


if __name__ == "__main__":
    result = query_real_gemini_cost()
    if result is None:
        print("Real billing data not available yet — export not linked, or first daily cycle hasn't landed.")
    else:
        print(result)
