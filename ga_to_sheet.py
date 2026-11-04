
import os
import json
import pandas as pd
import gspread

from datetime import date, timedelta
from google.oauth2 import service_account
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    RunReportRequest,
    DateRange,
    Dimension,
    Metric
)




PROPERTY_ID = os.environ["GA_PROPERTY_ID"].strip()
SHEET_ID = os.environ["SHEET_ID"].strip()
TAB_NAME = "ga_data"

creds_info = json.loads(os.environ["GA_CREDENTIALS"])

DIMS = [
    "date",
    "sessionSource",
    "sessionMedium"
]

METRICS = [
    "sessions",
    "activeUsers",
    "keyEvents"
]

KEY = [
    "date",
    "sessionSource",
    "sessionMedium"
]

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]




def validate_config():

    required_fields = [
        "type",
        "client_email",
        "private_key",
        "token_uri"
    ]

    for field in required_fields:
        if not creds_info.get(field):
            raise ValueError(
                f"Missing '{field}' in GA_CREDENTIALS"
            )

    if creds_info["type"] != "service_account":
        raise ValueError(
            "GA_CREDENTIALS must contain a service account JSON"
        )

    if not PROPERTY_ID:
        raise ValueError("GA_PROPERTY_ID is missing")

    if not SHEET_ID:
        raise ValueError("SHEET_ID is missing")

    print("Configuration validated.")
    print("Service account:", creds_info["client_email"])




def get_credentials():

    return service_account.Credentials.from_service_account_info(
        creds_info,
        scopes=SCOPES
    )



# pulling data from google analytics dashboard
def pull_ga(days_back=3):

    print("\nConnecting to Google Analytics...")

    credentials = get_credentials()

    client = BetaAnalyticsDataClient(
        credentials=credentials
    )

    start_date = (
        date.today() - timedelta(days=days_back)
    ).isoformat()

    end_date = (
        date.today() - timedelta(days=1)
    ).isoformat()

    print(f"Fetching data from {start_date} to {end_date}")

    request = RunReportRequest(
        property=f"properties/{PROPERTY_ID}",

        dimensions=[
            Dimension(name=d)
            for d in DIMS
        ],

        metrics=[
            Metric(name=m)
            for m in METRICS
        ],

        date_ranges=[
            DateRange(
                start_date=start_date,
                end_date=end_date
            )
        ],

        limit=100000
    )

    response = client.run_report(request)

    rows = [
        [d.value for d in row.dimension_values]
        + [m.value for m in row.metric_values]
        for row in response.rows
    ]

    df = pd.DataFrame(
        rows,
        columns=DIMS + METRICS
    )

    if df.empty:
        print("No GA4 data returned for this date range.")
        return df

    df["date"] = pd.to_datetime(
        df["date"],
        format="%Y%m%d"
    ).dt.strftime("%Y-%m-%d")

    for metric in METRICS:
        df[metric] = pd.to_numeric(
            df[metric],
            errors="coerce"
        ).fillna(0)

    print(f"GA4 extraction successful: {len(df)} rows")

    return df


# adding additional feature columns

def add_features(df):

    df = df.copy()

    if df.empty:
        return df

    d = pd.to_datetime(df["date"])

    df["day_name"] = d.dt.day_name()

    df["week"] = (
        d.dt.isocalendar().week.astype(int)
    )

    df["month"] = d.dt.strftime("%Y-%m")

    df["channel"] = (
        df["sessionSource"].astype(str)
        + " / "
        + df["sessionMedium"].astype(str)
    )

    df["conversion_rate"] = (
        df["keyEvents"]
        / df["sessions"].where(df["sessions"] > 0)
    ).round(4).fillna(0)

    df["sessions_per_user"] = (
        df["sessions"]
        / df["activeUsers"].where(df["activeUsers"] > 0)
    ).round(2).fillna(0)

    return df



# connecting google sheet
def connect_google_sheet():

    print("\nConnecting to Google Sheets...")

    credentials = get_credentials()

    gc = gspread.authorize(credentials)

    try:

        sh = gc.open_by_key(SHEET_ID)

        print("Google Sheet connected successfully.")

        return sh

    except gspread.exceptions.SpreadsheetNotFound:

        raise RuntimeError(
            "Spreadsheet not found or access denied. "
            "Check SHEET_ID and share the Google Sheet "
            "with the service account email as Editor."
        )

    except gspread.exceptions.APIError as e:

        raise RuntimeError(
            f"Google Sheets API error: {e}. "
            "Check Google Sheets API, Google Drive API, "
            "service account permissions and SHEET_ID."
        ) from e


#open google sheet

def get_worksheet(sh):

    try:

        ws = sh.worksheet(TAB_NAME)

        print(f"Worksheet '{TAB_NAME}' found.")

    except gspread.WorksheetNotFound:

        print(f"Creating worksheet '{TAB_NAME}'...")

        ws = sh.add_worksheet(
            title=TAB_NAME,
            rows=1000,
            cols=20
        )

    return ws



# pushing data into google sheet
def push_to_sheet(new_df):

    if new_df.empty:

        print("No new GA4 data to upload.")

        return

    print("\nPreparing Google Sheets upload...")

    sh = connect_google_sheet()

    ws = get_worksheet(sh)

    base_cols = DIMS + METRICS

    # Read existing data

    old_records = ws.get_all_records()

    old = pd.DataFrame(old_records)

    # Merge existing and new data

    if not old.empty:

        missing_cols = [
            col for col in base_cols
            if col not in old.columns
        ]

        if missing_cols:

            raise ValueError(
                f"Existing sheet is missing columns: {missing_cols}"
            )

        old = old[base_cols].copy()

        old["date"] = pd.to_datetime(
            old["date"],
            errors="coerce"
        ).dt.strftime("%Y-%m-%d")

        for metric in METRICS:

            old[metric] = pd.to_numeric(
                old[metric],
                errors="coerce"
            ).fillna(0)

        # Remove dates being refreshed

        old = old[
            ~old["date"].isin(
                new_df["date"].unique()
            )
        ]

        combined = pd.concat(
            [old, new_df[base_cols]],
            ignore_index=True
        )

    else:

        combined = new_df[base_cols].copy()

    # Remove duplicates

    combined = combined.drop_duplicates(
        subset=KEY,
        keep="last"
    )

    # Sort records

    combined = combined.sort_values(
        ["date", "sessionSource", "sessionMedium"]
    ).reset_index(drop=True)

    # Add features

    combined = add_features(combined)

    # Prepare Google Sheets values

    values = [
        combined.columns.tolist()
    ] + combined.fillna("").values.tolist()

    # Expand worksheet if required

    required_rows = len(values)
    required_cols = len(values[0])

    if ws.row_count < required_rows or ws.col_count < required_cols:

        ws.resize(
            rows=max(ws.row_count, required_rows),
            cols=max(ws.col_count, required_cols)
        )

    # Upload

    ws.clear()

    ws.update(
        values=values,
        range_name="A1"
    )

    print("\n================================")
    print("GOOGLE SHEETS UPLOAD SUCCESSFUL")
    print("================================")

    print("Total rows:", len(combined))
    print("Worksheet:", TAB_NAME)


# main execution

def main():

    print("\n================================")
    print("GA4 DAILY AUTOMATION STARTED")
    print("================================")

    validate_config()

    df = pull_ga(days_back=3)

    if df.empty:

        print("Automation completed. No data available.")

        return

    push_to_sheet(df)

    print("\nAutomation completed successfully!")


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print("\nAUTOMATION FAILED!")
        print("Error:", repr(e))

        raise
