#!/usr/bin/env python3
"""
================================================================================
 GOOGLE ADS API  →  campaign ↔ app ka ASLI mapping                     (v1.0)
================================================================================
 🔑 YE KYUN BANI

 Abhi mapping teen jugaadon par chalti hai:
     package_correction_map   (haath se)
     conversion_action_map    (conversion action ka ID — magar naye actions
                               add nahi hote, is liye UNMAPPED reh jate hain)
     orphan_campaign_map      (haath se)

 Nateeja (July 2026):  43 campaigns · $4,778.39 UNMAPPED
 Aur us ki wajah se kai apps par installs to aate hain magar UA cost $0:
     Ai Voice Changer  5,126 installs · UA $0
     Smiley Face       1,430 installs · UA $0

 ✅ YE SCRIPT GOOGLE KA APNA FIELD LAATI HAI:
        campaign.app_campaign_setting.app_id
        campaign.app_campaign_setting.app_store

    Android  →  app_id = com.package.name
    iOS      →  app_id = 6783663881   (numeric Apple ID)

    Ye naam parhna nahi — Google ka asli link hai. 100% durust.

 ⚠️ BigQuery Data Transfer ye field NAHI laati (schema fixed hai) —
    is liye API se lena parta hai.

================================================================================
 SETUP — ek baar (guide ke liye GOOGLE_ADS_API_SETUP.md dekhein)
================================================================================
   GADS_DEVELOPER_TOKEN     MCC → Tools & Settings → Setup → API Center
   GADS_CLIENT_ID           Google Cloud → OAuth 2.0 Client ID (Desktop app)
   GADS_CLIENT_SECRET
   GADS_REFRESH_TOKEN       ek baar OAuth flow se
   GADS_LOGIN_CUSTOMER_ID   MCC ka ID (dashes ke bagair)
   GCP_PROJECT · GCP_CREDENTIALS_JSON

 OUTPUT: terafort.Google_ads_master.gads_campaign_app_map
     campaign_id · customer_id · campaign_name · app_id · app_store
     · platform · android_package · apple_id · channel_sub_type
     · campaign_status · _loaded_at
================================================================================
"""
import json
import os
import sys
from datetime import datetime, timezone

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.cloud import bigquery
from google.oauth2 import service_account

# ─── CONFIG ──────────────────────────────────────────────────────────────────
DEV_TOKEN     = os.environ["GADS_DEVELOPER_TOKEN"].strip()
CLIENT_ID     = os.environ["GADS_CLIENT_ID"].strip()
CLIENT_SECRET = os.environ["GADS_CLIENT_SECRET"].strip()
REFRESH_TOKEN = os.environ["GADS_REFRESH_TOKEN"].strip()
LOGIN_CID     = os.environ["GADS_LOGIN_CUSTOMER_ID"].strip().replace("-", "")

GCP_PROJECT   = os.environ["GCP_PROJECT"].strip()
BQ_DATASET    = os.environ.get("BQ_DATASET", "Google_ads_master").strip()
GCP_CREDS     = os.environ["GCP_CREDENTIALS_JSON"]

TABLE = "gads_campaign_app_map"


def log(m):
    print(m, flush=True)


def fail(m):
    log(f"\n🚨 FAILED: {m}")
    sys.exit(1)


# ─── GOOGLE ADS CLIENT ───────────────────────────────────────────────────────
def get_client() -> GoogleAdsClient:
    return GoogleAdsClient.load_from_dict({
        "developer_token":    DEV_TOKEN,
        "client_id":          CLIENT_ID,
        "client_secret":      CLIENT_SECRET,
        "refresh_token":      REFRESH_TOKEN,
        "login_customer_id":  LOGIN_CID,
        "use_proto_plus":     True,
    })


def list_child_accounts(client) -> list:
    """
    MCC ke neeche saare CHALU (non-manager) accounts.
    Aise naye account bhi khud aa jate hain — hardcode nahi karna parta.
    """
    svc = client.get_service("GoogleAdsService")
    q = """
        SELECT customer_client.id,
               customer_client.descriptive_name,
               customer_client.manager,
               customer_client.status
        FROM customer_client
        WHERE customer_client.status = 'ENABLED'
          AND customer_client.manager = FALSE
    """
    out = []
    for row in svc.search(customer_id=LOGIN_CID, query=q):
        out.append({
            "id":   str(row.customer_client.id),
            "name": row.customer_client.descriptive_name or "",
        })
    log(f"  {len(out)} chalu accounts mile MCC {LOGIN_CID} ke neeche")
    return out


# ─── ASAL KAAM ───────────────────────────────────────────────────────────────
def fetch_campaign_apps(client, cid: str, name: str) -> tuple:
    """
    🔑 campaign.app_campaign_setting.app_id  —  Google ka apna link.

    App campaigns:
        MULTI_CHANNEL (App Install / App Engagement / Pre-registration)
    """
    svc = client.get_service("GoogleAdsService")
    q = """
        SELECT campaign.id,
               campaign.name,
               campaign.status,
               campaign.advertising_channel_type,
               campaign.advertising_channel_sub_type,
               campaign.app_campaign_setting.app_id,
               campaign.app_campaign_setting.app_store
        FROM campaign
        WHERE campaign.advertising_channel_type = 'MULTI_CHANNEL'
          AND campaign.status IN ('ENABLED', 'PAUSED')
    """
    rows, now = [], datetime.now(timezone.utc).isoformat()
    try:
        for r in svc.search(customer_id=cid, query=q):
            s = r.campaign.app_campaign_setting
            app_id = (s.app_id or "").strip()
            if not app_id:
                continue                      # setting hi nahi — skip

            store = s.app_store.name if s.app_store else ""
            is_ios = (store == "APPLE_APP_STORE") or app_id.isdigit()

            rows.append({
                "campaign_id":       str(r.campaign.id),
                "customer_id":       cid,
                "customer_name":     name,
                "campaign_name":     r.campaign.name,
                "campaign_status":   r.campaign.status.name,
                "channel_sub_type":  r.campaign.advertising_channel_sub_type.name,
                "app_id":            app_id,
                "app_store":         store,
                "platform":          "ios" if is_ios else "android",
                # 🔑 alag alag columns — join karna aasan ho jaye
                "android_package":   None if is_ios else app_id,
                "apple_id":          app_id if is_ios else None,
                "_loaded_at":        now,
            })
    except GoogleAdsException as ex:
        # ek account fail ho to poori run na rukay — magar CHUP bhi na rahe
        errs = "; ".join(e.message for e in ex.failure.errors[:2])
        log(f"  🔴 {name} ({cid}): {errs}")
        return [], 1
    except Exception as exc:
        log(f"  🔴 {name} ({cid}): {type(exc).__name__}: {exc}")
        return [], 1

    if rows:
        log(f"  ✅ {name:<28} ({cid})  {len(rows):>4} app campaigns")
    return rows, 0


# ─── BIGQUERY ────────────────────────────────────────────────────────────────
def get_bq():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(GCP_CREDS),
        scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return bigquery.Client(project=GCP_PROJECT, credentials=creds)


SCHEMA = [
    bigquery.SchemaField("campaign_id",      "STRING"),
    bigquery.SchemaField("customer_id",      "STRING"),
    bigquery.SchemaField("customer_name",    "STRING"),
    bigquery.SchemaField("campaign_name",    "STRING"),
    bigquery.SchemaField("campaign_status",  "STRING"),
    bigquery.SchemaField("channel_sub_type", "STRING"),
    bigquery.SchemaField("app_id",           "STRING"),
    bigquery.SchemaField("app_store",        "STRING"),
    bigquery.SchemaField("platform",         "STRING"),
    bigquery.SchemaField("android_package",  "STRING"),
    bigquery.SchemaField("apple_id",         "STRING"),
    bigquery.SchemaField("_loaded_at",       "TIMESTAMP"),
]


def load_bq(bq, rows: list):
    """
    🛡️ ATOMIC: pehle temp table, phir TRANSACTION mein swap.
       Beech mein fail ho to PURANI TABLE SALAMAT.
    """
    target = f"{GCP_PROJECT}.{BQ_DATASET}.{TABLE}"
    tmp    = f"{GCP_PROJECT}.{BQ_DATASET}._tmp_{TABLE}"

    bq.load_table_from_json(
        rows, tmp,
        job_config=bigquery.LoadJobConfig(schema=SCHEMA,
                                          write_disposition="WRITE_TRUNCATE"),
    ).result()

    try:
        try:
            bq.get_table(target)
            bq.query(f"""
                BEGIN TRANSACTION;
                  DELETE FROM `{target}` WHERE TRUE;
                  INSERT INTO `{target}` SELECT * FROM `{tmp}`;
                COMMIT TRANSACTION;
            """).result()
        except Exception:
            bq.query(f"CREATE TABLE `{target}` AS SELECT * FROM `{tmp}`").result()
            log(f"  🆕 table bani: {TABLE}")
    finally:
        bq.query(f"DROP TABLE IF EXISTS `{tmp}`").result()


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    log("🎯 Google Ads API → campaign ↔ app mapping")
    log(f"   MCC: {LOGIN_CID}  ·  dataset: {BQ_DATASET}\n")

    client = get_client()

    # 🔍 kaunsi API version use ho rahi hai — log mein saaf nazar aaye.
    #    Google Ads ki har version ~12 mahine chalti hai; sunset hone par
    #    "501 GRPC target method can't be resolved" aata hai.
    #    (v19 11-Feb-2026 ko sunset hui thi — wahi masla tha)
    try:
        from google.ads.googleads import client as _gac
        log(f"   Google Ads API version: {_gac.GoogleAdsClient.get_default_version()}")
    except Exception:
        try:
            import google.ads.googleads as _ga
            log(f"   google-ads library: {_ga.VERSION}")
        except Exception:
            pass

    log("\n── accounts ────────────────────────────────────────")
    try:
        accounts = list_child_accounts(client)
    except GoogleAdsException as ex:
        fail("MCC ke accounts nahi mile — "
             + "; ".join(e.message for e in ex.failure.errors[:2])
             + "\n   → developer token approve hua? login_customer_id sahi hai?")
    except Exception as exc:
        if "can't be resolved" in str(exc) or "UNIMPLEMENTED" in str(exc):
            fail("🔴 API VERSION SUNSET HO CHUKI HAI\n"
                 "   Google Ads ki har version ~12 mahine chalti hai.\n"
                 "   → requirements.txt mein `google-ads` par UPPER CAP na rakhein\n"
                 "   → phir: pip install --upgrade google-ads\n"
                 f"   asal error: {exc}")
        fail(f"MCC ke accounts nahi mile — {type(exc).__name__}: {exc}")
    if not accounts:
        fail("MCC ke neeche koi chalu account nahi mila")

    log("\n── campaigns ───────────────────────────────────────")
    all_rows, failures = [], 0
    for a in accounts:
        rows, f = fetch_campaign_apps(client, a["id"], a["name"])
        all_rows.extend(rows)
        failures += f

    if not all_rows:
        fail("kisi bhi account se app campaign nahi mila — "
             "developer token / permissions check karein")

    # ── duplicate campaign_id na jayen ──
    seen, uniq = set(), []
    for r in all_rows:
        k = (r["campaign_id"], r["customer_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)

    log(f"\n── BigQuery ────────────────────────────────────────")
    bq = get_bq()
    load_bq(bq, uniq)

    n_and = sum(1 for r in uniq if r["platform"] == "android")
    n_ios = len(uniq) - n_and
    log(f"  ✅ {len(uniq):,} campaigns → {TABLE}")
    log(f"     android {n_and:,}  ·  ios {n_ios:,}  ·  accounts {len(accounts)}")

    if failures:
        log(f"\n⚠️  {failures} account fail hue — upar log dekhein")
        sys.exit(1)
    log("\n✅ done")


if __name__ == "__main__":
    main()
