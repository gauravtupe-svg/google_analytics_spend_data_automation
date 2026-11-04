import os
import json
import logging
from datetime import date, timedelta

import pandas as pd
import gspread

from google.oauth2.service_account import Credentials
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)


# ==================================================
# 1. CONFIGURATION
# ==================================================

PROPERTY_ID = os.environ["GA_PROPERTY_ID"].strip()
SHEET_ID = os.environ["SHEET_ID"].strip()

DAYS_BACK = int(os.getenv("DAYS_BACK", "3"))

MAIN_TAB = "ga_data"
ADS_TAB = "ga_ads_cost"

if DAYS_BACK < 1:
    raise ValueError("DAYS_BACK must be at least 1.")

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# ==================================================
# 2. GA4 DIMENSIONS
# ==================================================

MAIN_DIMENSIONS = [
    "date",
    "sessionCampaignName",
    "sessionSource",
    "sessionMedium",
    "sessionDefaultChannelGroup",
]


ADS_DIMENSIONS = [
    "date",
    "sessionCampaignName",
]


# ==================================================
# 3. GA4 METRICS
# ==================================================

# GA4 allows a maximum of 10 metrics per request.
# Therefore, the main report is split into two requests.

MAIN_METRICS = [
    "sessions",
    "totalUsers",
    "activeUsers",
    "newUsers",
    "engagedSessions",
    "engagementRate",
    "bounceRate",
    "screenPageViews",
    "eventCount",
    "keyEvents",
]

EXTRA_METRICS = [
    "purchaseRevenue",
    "totalRevenue",
]


# Advertising metrics are fetched separately.

ADS_METRICS = [
    "advertiserAdCost",
    "advertiserAdClicks",
    "advertiserAdImpressions",
    "advertiserAdCostPerClick",
    "returnOnAdSpend",
]


MAIN_KEYS = MAIN_DIMENSIONS.copy()

ADS_KEYS = ADS_DIMENSIONS.copy()


# ==================================================
# 4. GOOGLE AUTHENTICATION
# ==================================================

def get_credentials():

    credentials_json = os.environ.get("GA_CREDENTIALS")

    if not credentials_json:
        raise ValueError("GA_CREDENTIALS secret is missing.")

    credentials_info = json.loads(credentials_json)

    if credentials_info.get("type") != "service_account":
        raise ValueError(
            "GA_CREDENTIALS must contain service account JSON."
        )

    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=SCOPES,
    )

    return credentials


def connect_services():

    credentials = get_credentials()

    logging.info("Connecting to GA4...")

    ga_client = BetaAnalyticsDataClient(
        credentials=credentials
    )

    logging.info("Connecting to Google Sheets...")

    sheets_client = gspread.authorize(credentials)

    spreadsheet = sheets_client.open_by_key(SHEET_ID)

    logging.info("Connected successfully.")

    return ga_client, spreadsheet


# ==================================================
# 5. GENERIC GA4 REPORT FUNCTION
# ==================================================

def fetch_ga4_report(
    client,
    dimensions,
    metrics,
    start_date,
    end_date,
):

    all_rows = []

    offset = 0
    limit = 100000

    while True:

        request = RunReportRequest(
            property=f"properties/{PROPERTY_ID}",

            dimensions=[
                Dimension(name=name)
                for name in dimensions
            ],

            metrics=[
                Metric(name=name)
                for name in metrics
            ],

            date_ranges=[
                DateRange(
                    start_date=start_date,
                    end_date=end_date,
                )
            ],

            limit=limit,
            offset=offset,
        )

        response = client.run_report(request)

        for row in response.rows:

            record = {}

            # Dimensions
            for index, dimension in enumerate(dimensions):

                record[dimension] = (
                    row.dimension_values[index].value
                )

            # Metrics
            for index, metric in enumerate(metrics):

                record[metric] = (
                    row.metric_values[index].value
                )

            all_rows.append(record)

        logging.info(
            "Fetched %s rows. Total: %s",
            len(response.rows),
            len(all_rows),
        )

        if len(response.rows) < limit:
            break

        offset += limit

    return pd.DataFrame(all_rows)


# ==================================================
# 6. FETCH MAIN GA4 DATA
# ==================================================

def pull_main_data(client):

    today = date.today()

    # Exclude today's incomplete data.
    end_date = today - timedelta(days=1)

    start_date = end_date - timedelta(
        days=DAYS_BACK - 1
    )

    logging.info(
        "Fetching main GA4 data from %s to %s",
        start_date,
        end_date,
    )

    # ----------------------------------------------
    # REQUEST 1: First 10 metrics
    # ----------------------------------------------

    logging.info("Fetching main metrics...")

    df_main = fetch_ga4_report(
        client=client,
        dimensions=MAIN_DIMENSIONS,
        metrics=MAIN_METRICS,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    if df_main.empty:
        logging.warning("No main GA4 data found.")
        return pd.DataFrame()

    # ----------------------------------------------
    # REQUEST 2: Remaining 2 metrics
    # ----------------------------------------------

    logging.info("Fetching revenue metrics...")

    df_extra = fetch_ga4_report(
        client=client,
        dimensions=MAIN_DIMENSIONS,
        metrics=EXTRA_METRICS,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    # ----------------------------------------------
    # MERGE BOTH REPORTS
    # ----------------------------------------------

    if not df_extra.empty:

        df_main = df_main.merge(
            df_extra,
            on=MAIN_DIMENSIONS,
            how="left",
        )

    else:

        for metric in EXTRA_METRICS:
            df_main[metric] = 0

    # Missing revenue values become zero.
    for metric in EXTRA_METRICS:
        df_main[metric] = (
            pd.to_numeric(
                df_main[metric],
                errors="coerce",
            ).fillna(0)
        )

    logging.info(
        "Main GA4 report completed: %s rows.",
        len(df_main),
    )

    return df_main


# ==================================================
# 7. FETCH ADVERTISING DATA
# ==================================================

def pull_ads_data(client):

    today = date.today()

    end_date = today - timedelta(days=1)

    start_date = end_date - timedelta(
        days=DAYS_BACK - 1
    )

    logging.info(
        "Fetching advertising data from %s to %s",
        start_date,
        end_date,
    )

    try:

        df = fetch_ga4_report(
            client=client,
            dimensions=ADS_DIMENSIONS,
            metrics=ADS_METRICS,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
        )

        logging.info(
            "Advertising report completed: %s rows.",
            len(df),
        )

        return df

    except Exception as error:

        logging.warning(
            "Advertising report unavailable: %s",
            error,
        )

        return pd.DataFrame(
            columns=ADS_DIMENSIONS + ADS_METRICS
        )


# ==================================================
# 8. DATA CLEANING
# ==================================================

def clean_data(df):

    if df.empty:
        return df

    df = df.copy()

    # Convert GA4 date.
    if "date" in df.columns:

        df["date"] = pd.to_datetime(
            df["date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")

    # Convert metric columns to numbers.
    dimension_columns = (
        MAIN_DIMENSIONS + ADS_DIMENSIONS
    )

    for column in df.columns:

        if column == "date":
            continue

        if column in dimension_columns:
            continue

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        ).fillna(0)

    return df


# ==================================================
# 9. ADD MAIN REPORT FEATURES
# ==================================================

def add_main_features(df):

    if df.empty:
        return df

    df = df.copy()

    date_column = pd.to_datetime(
        df["date"],
        errors="coerce",
    )

    df["day_name"] = date_column.dt.day_name()

    df["week"] = (
        date_column.dt.isocalendar().week
    )

    df["month"] = date_column.dt.strftime("%Y-%m")

    # Conversion rate.
    df["conversion_rate"] = (
        df["keyEvents"]
        .div(df["sessions"].replace(0, float("nan")))
        .fillna(0)
    )

    # Sessions per user.
    df["sessions_per_user"] = (
        df["sessions"]
        .div(df["totalUsers"].replace(0, float("nan")))
        .fillna(0)
    )

    return df


# ==================================================
# 10. ADD ADVERTISING FEATURES
# ==================================================

def add_ads_features(df):

    if df.empty:
        return df

    df = df.copy()

    df["cost"] = df["advertiserAdCost"]

    df["clicks"] = df["advertiserAdClicks"]

    df["impressions"] = df["advertiserAdImpressions"]

    return df


# ==================================================
# 11. GOOGLE SHEETS WORKSHEET
# ==================================================

def get_or_create_worksheet(
    spreadsheet,
    title,
):

    try:

        worksheet = spreadsheet.worksheet(title)

    except gspread.WorksheetNotFound:

        logging.info(
            "Creating worksheet: %s",
            title,
        )

        worksheet = spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=50,
        )

    return worksheet


# ==================================================
# 12. READ EXISTING SHEET DATA
# ==================================================

def read_existing_data(worksheet):

    records = worksheet.get_all_records()

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records)


# ==================================================
# 13. MERGE OLD AND NEW DATA
# ==================================================

def merge_with_existing(
    new_df,
    old_df,
    key_columns,
):

    new_df = new_df.copy()

    if old_df.empty:
        return new_df

    old_df = old_df.copy()

    # Add missing columns to old data.
    for column in new_df.columns:

        if column not in old_df.columns:
            old_df[column] = pd.NA

    # Add missing columns to new data.
    for column in old_df.columns:

        if column not in new_df.columns:
            new_df[column] = pd.NA

    # Match column structure.
    old_df = old_df[new_df.columns]

    # Remove dates that are being refreshed.
    refreshed_dates = (
        new_df["date"]
        .dropna()
        .astype(str)
        .unique()
    )

    old_df = old_df[
        ~old_df["date"]
        .astype(str)
        .isin(refreshed_dates)
    ]

    # Combine old and new records.
    combined = pd.concat(
        [old_df, new_df],
        ignore_index=True,
    )

    # Remove duplicates.
    combined = combined.drop_duplicates(
        subset=key_columns,
        keep="last",
    )

    # Sort data.
    combined = combined.sort_values(
        by=key_columns,
        na_position="last",
    )

    return combined


# ==================================================
# 14. PREPARE GOOGLE SHEETS VALUES
# ==================================================

def prepare_sheet_values(df):

    df = df.copy()

    df = df.astype(object)

    df = df.where(
        pd.notna(df),
        "",
    )

    values = [
        df.columns.tolist()
    ]

    for row in df.values.tolist():

        clean_row = []

        for value in row:

            if hasattr(value, "item"):
                value = value.item()

            clean_row.append(value)

        values.append(clean_row)

    return values


# ==================================================
# 15. WRITE DATA TO GOOGLE SHEETS
# ==================================================

def write_to_sheet(
    spreadsheet,
    df,
    tab_name,
):

    if df.empty:

        logging.warning(
            "No data available for tab %s.",
            tab_name,
        )

        return

    worksheet = get_or_create_worksheet(
        spreadsheet,
        tab_name,
    )

    old_df = read_existing_data(worksheet)

    if tab_name == MAIN_TAB:

        keys = MAIN_KEYS

    else:

        keys = ADS_KEYS

    combined_df = merge_with_existing(
        new_df=df,
        old_df=old_df,
        key_columns=keys,
    )

    values = prepare_sheet_values(
        combined_df
    )

    # Resize worksheet.
    worksheet.resize(
        rows=max(len(values), 100),
        cols=max(len(values[0]), 1),
    )

    # Clear previous contents.
    worksheet.clear()

    # Write updated data.
    worksheet.update(
        values=values,
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

    logging.info(
        "Successfully updated %s.",
        tab_name,
    )

    logging.info(
        "Rows: %s | Columns: %s",
        len(combined_df),
        len(combined_df.columns),
    )


# ==================================================
# 16. MAIN EXECUTION
# ==================================================

def main():

    logging.info(
        "Starting GA4 to Google Sheets automation."
    )

    if not PROPERTY_ID:
        raise ValueError("GA_PROPERTY_ID is missing.")

    if not SHEET_ID:
        raise ValueError("SHEET_ID is missing.")

    # Connect to services.
    ga_client, spreadsheet = connect_services()

    # ----------------------------------------------
    # MAIN GA4 DATA
    # ----------------------------------------------

    main_df = pull_main_data(
        ga_client
    )

    if not main_df.empty:

        main_df = clean_data(
            main_df
        )

        main_df = add_main_features(
            main_df
        )

        write_to_sheet(
            spreadsheet=spreadsheet,
            df=main_df,
            tab_name=MAIN_TAB,
        )

    # ----------------------------------------------
    # ADVERTISING DATA
    # ----------------------------------------------

    ads_df = pull_ads_data(
        ga_client
    )

    if not ads_df.empty:

        ads_df = clean_data(
            ads_df
        )

        ads_df = add_ads_features(
            ads_df
        )

        write_to_sheet(
            spreadsheet=spreadsheet,
            df=ads_df,
            tab_name=ADS_TAB,
        )

    logging.info(
        "GA4 automation completed successfully."
    )


# ==================================================
# 17. RUN SCRIPT
# ==================================================

if __name__ == "__main__":

    try:

        main()

    except Exception:

        logging.exception(
            "GA4 automation failed."
        )

        raise
