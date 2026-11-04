import os, json
import pandas as pd
import gspread
from datetime import date, timedelta
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest, DateRange, Dimension, Metric
)

PROPERTY_ID = os.environ["GA_PROPERTY_ID"]
SHEET_ID = os.environ["SHEET_ID"]
TAB_NAME = "ga_data"
creds_info = json.loads(os.environ["GA_CREDENTIALS"])

DIMS = ["date", "sessionSource", "sessionMedium"]
METRICS = ["sessions", "activeUsers", "keyEvents"]  # if this errors, use "conversions"
KEY = ["date", "sessionSource", "sessionMedium"]


# 1. PULL FROM GA4
def pull_ga(days_back=3):
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/analytics.readonly"]
    )
    client = BetaAnalyticsDataClient(credentials=creds)
    req = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",
        dimensions=[Dimension(name=d) for d in DIMS],
        metrics=[Metric(name=m) for m in METRICS],
        date_ranges=[DateRange(
            start_date=(date.today() - timedelta(days=days_back)).isoformat(),
            end_date=(date.today() - timedelta(days=1)).isoformat(),
        )],
        limit=100000,
    )
    resp = client.run_report(req)
    rows = [[d.value for d in r.dimension_values] + [m.value for m in r.metric_values]
            for r in resp.rows]
    df = pd.DataFrame(rows, columns=DIMS + METRICS)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    for m in METRICS:
        df[m] = pd.to_numeric(df[m])
    return df


# 2. ADD YOUR NEW COLUMNS (edit this function)
def add_features(df):
    d = pd.to_datetime(df["date"])
    df["day_name"] = d.dt.day_name()
    df["week"] = d.dt.isocalendar().week.astype(int)
    df["month"] = d.dt.strftime("%Y-%m")
    df["channel"] = df["sessionSource"] + " / " + df["sessionMedium"]
    df["conversion_rate"] = (df["keyEvents"] / df["sessions"].where(df["sessions"] > 0)).round(4).fillna(0)
    df["sessions_per_user"] = (df["sessions"] / df["activeUsers"].where(df["activeUsers"] > 0)).round(2).fillna(0)
    return df


# 3. PUSH TO GOOGLE SHEET (replaces the re-pulled days, keeps older rows)
def push_to_sheet(new_df):
    gc = gspread.service_account_from_dict(creds_info)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(TAB_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TAB_NAME, rows=1000, cols=20)

    base_cols = DIMS + METRICS
    old = pd.DataFrame(ws.get_all_records())
    if not old.empty:
        old = old[base_cols]
        for m in METRICS:
            old[m] = pd.to_numeric(old[m])
        old = old[~old["date"].isin(new_df["date"].unique())]  # drop days being refreshed
        combined = pd.concat([old, new_df[base_cols]], ignore_index=True)
    else:
        combined = new_df[base_cols].copy()

    combined = combined.drop_duplicates(subset=KEY, keep="last")
    combined = combined.sort_values(["date", "sessionSource"]).reset_index(drop=True)
    combined = add_features(combined)

    values = [combined.columns.tolist()] + json.loads(combined.to_json(orient="values"))
    ws.clear()
    ws.update(values, "A1")
    print(f"Sheet updated: {len(combined)} rows")


if __name__ == "__main__":
    df = pull_ga()
    push_to_sheet(df)
