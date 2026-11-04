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

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

PROPERTY_ID = os.environ["GA_PROPERTY_ID"].strip()
SHEET_ID = os.environ["SHEET_ID"].strip()

DAYS_BACK = int(os.getenv("DAYS_BACK", "3"))
MAIN_TAB = os.getenv("MAIN_TAB", "ga_data")
ADS_TAB = os.getenv("ADS_TAB", "ga_ads_cost")

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


# --------------------------------------------------
# GA4 REPORT CONFIGURATION
# --------------------------------------------------

# Main traffic and campaign report
MAIN_DIMENSIONS = [
    "date",
    "sessionCampaignName",
    "sessionSource",
    "sessionMedium",
    "sessionDefaultChannelGroup",
]

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
    "purchaseRevenue",
    "totalRevenue",
]

MAIN_KEYS = [
    "date",
    "sessionCampaignName",
    "sessionSource",
    "sessionMedium",
    "sessionDefaultChannelGroup",
]

# Advertising cost report.
# This is kept in a separate tab to avoid duplicating campaign spend
# across multiple source/medium rows in the main report.
ADS_DIMENSIONS = [
    "date",
    "sessionCampaignName",
]

ADS_METRICS = [
    "advertiserAdCost",
    "advertiserAdClicks",
    "advertiserAdImpressions",
    "advertiserAdCostPerClick",
    "returnOnAdSpend",
]

ADS_KEYS = [
    "date",
    "sessionCampaignName",
]


# --------------------------------------------------
# AUTHENTICATION
# --------------------------------------------------

def get_credentials():
    """Load service account credentials from GitHub secret."""

    credentials_json = os.environ.get("GA_CREDENTIALS")

    if not credentials_json:
        raise ValueError("GA_CREDENTIALS secret is missing.")

    credentials_info = json.loads(credentials_json)

    if credentials_info.get("type") != "service_account":
        raise ValueError(
            "GA_CREDENTIALS must contain service account JSON, "
            "not an API key."
        )

    return Credentials.from_service_account_info(
        credentials_info,
        scopes=SCOPES,
    )


def connect_services():
    """Connect to GA4 and Google Sheets."""

    credentials = get_credentials()

    ga_client = BetaAnalyticsDataClient(
        credentials=credentials
    )

    sheets_client = gspread.authorize(credentials)

    spreadsheet = sheets_client.open_by_key(SHEET_ID)

    logging.info("Connected to GA4 and Google Sheets.")

    return ga_client, spreadsheet


# --------------------------------------------------
# GA4 DATA EXTRACTION
# --------------------------------------------------

def fetch_ga4_report(
    client,
    dimensions,
    metrics,
    start_date,
    end_date,
):
    """
    Fetch a GA4 report, including pagination.
    Returns a pandas DataFrame.
    """

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

            for index, dimension in enumerate(dimensions):
                record[dimension] = (
                    row.dimension_values[index].value
                )

            for index, metric in enumerate(metrics):
                record[metric] = (
                    row.metric_values[index].value
                )

            all_rows.append(record)

        logging.info(
            "Fetched %s rows. Total so far: %s",
            len(response.rows),
            len(all_rows),
        )

        if len(response.rows) < limit:
            break

        offset += limit

    return pd.DataFrame(all_rows)


def pull_main_data(client):
    """Fetch the main GA4 traffic and campaign report."""

    today = date.today()

    # Exclude today because GA4 data for today may be incomplete.
    end_date = today - timedelta(days=1)

    # DAYS_BACK=3 means the previous three complete days.
    start_date = end_date - timedelta(days=DAYS_BACK - 1)

    logging.info(
        "Fetching main GA4 data from %s to %s",
        start_date,
        end_date,
    )

    df = fetch_ga4_report(
        client=client,
        dimensions=MAIN_DIMENSIONS,
        metrics=MAIN_METRICS,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )

    return df


def pull_ads_data(client):
    """Fetch advertising cost, clicks and impressions."""

    today = date.today()
    end_date = today - timedelta(days=1)
    start_date = end_date - timedelta(days=DAYS_BACK - 1)

    logging.info(
        "Fetching GA4 advertising data from %s to %s",
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

        return df

    except Exception as error:
        logging.warning(
            "Advertising metrics could not be fetched. "
            "Check GA4 advertising integrations and metric compatibility. "
            "Details: %s",
            error,
        )

        return pd.DataFrame(
            columns=ADS_DIMENSIONS + ADS_METRICS
        )


# --------------------------------------------------
# DATA CLEANING AND FEATURES
# --------------------------------------------------

def clean_data(df):
    """Convert GA4 values into useful data types."""

    if df.empty:
        return df

    df = df.copy()

    # Format GA4 date from YYYYMMDD to YYYY-MM-DD.
    if "date" in df.columns:
        df["date"] = pd.to_datetime(
            df["date"],
            format="%Y%m%d",
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")

    # Convert numeric columns.
    for column in df.columns:

        if column == "date":
            continue

        if column in MAIN_DIMENSIONS or column in ADS_DIMENSIONS:
            continue

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        ).fillna(0)

    return df


def add_main_features(df):
    """Add calculated columns to the main report."""

    if df.empty:
        return df

    df = df.copy()

    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce",
    )

    df["day_name"] = df["date"].dt.day_name()
    df["week"] = df["date"].dt.isocalendar().week.astype("Int64")
    df["month"] = df["date"].dt.strftime("%Y-%m")

    df["conversion_rate"] = (
        df["keyEvents"] / df["sessions"].replace(0, pd.NA)
    ).fillna(0)

    df["sessions_per_user"] = (
        df["sessions"] / df["totalUsers"].replace(0, pd.NA)
    ).fillna(0)

    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    return df


def add_ads_features(df):
    """Add calculated columns to the advertising report."""

    if df.empty:
        return df

    df = df.copy()

    if "advertiserAdCost" in df.columns:
        df["cost"] = df["advertiserAdCost"]

    if "advertiserAdClicks" in df.columns:
        df["clicks"] = df["advertiserAdClicks"]

    if "advertiserAdImpressions" in df.columns:
        df["impressions"] = df["advertiserAdImpressions"]

    return df


# --------------------------------------------------
# GOOGLE SHEETS HELPERS
# --------------------------------------------------

def get_or_create_worksheet(spreadsheet, title):
    """Get a worksheet or create it if it doesn't exist."""

    try:
        return spreadsheet.worksheet(title)

    except gspread.WorksheetNotFound:
        logging.info("Creating worksheet: %s", title)

        return spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=50,
        )


def read_existing_data(worksheet):
    """Read existing worksheet data safely."""

    records = worksheet.get_all_records()

    if not records:
        return pd.DataFrame()

    return pd.DataFrame(records)


def merge_with_existing(
    new_df,
    old_df,
    key_columns,
):
    """
    Preserve older dates while replacing refreshed records.
    Handles changes to the worksheet column structure.
    """

    if old_df.empty:
        return new_df.copy()

    # Keep only columns that exist in the new report.
    # This avoids KeyError when the schema has changed.
    for column in new_df.columns:
        if column not in old_df.columns:
            old_df[column] = pd.NA

    for column in old_df.columns:
        if column not in new_df.columns:
            new_df[column] = pd.NA

    old_df = old_df[new_df.columns]

    # Remove old records for dates being refreshed.
    refreshed_dates = new_df["date"].dropna().unique()

    old_df = old_df[
        ~old_df["date"].astype(str).isin(
            refreshed_dates.astype(str)
        )
    ]

    combined = pd.concat(
        [old_df, new_df],
        ignore_index=True,
    )

    combined = combined.drop_duplicates(
        subset=key_columns,
        keep="last",
    )

    combined = combined.sort_values(
        by=key_columns,
        na_position="last",
    )

    return combined


def prepare_sheet_values(df):
    """Convert DataFrame into Google Sheets-compatible values."""

    df = df.copy()

    df = df.astype(object)
    df = df.where(pd.notna(df), "")

    values = [df.columns.tolist()]

    for row in df.values.tolist():
        values.append([
            value.item() if hasattr(value, "item") else value
            for value in row
        ])

    return values


def write_to_sheet(spreadsheet, df, tab_name):
    """Merge and write data into a Google Sheets tab."""

    if df.empty:
        logging.warning(
            "No new data to write to %s.",
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
        new_df=df.copy(),
        old_df=old_df.copy(),
        key_columns=keys,
    )

    values = prepare_sheet_values(combined_df)

    # Resize before writing.
    worksheet.resize(
        rows=max(len(values), 100),
        cols=max(len(values[0]), 1),
    )

    worksheet.clear()

    worksheet.update(
        values=values,
        range_name="A1",
        value_input_option="USER_ENTERED",
    )

    logging.info(
        "Updated tab '%s' with %s records and %s columns.",
        tab_name,
        len(combined_df),
        len(combined_df.columns),
    )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():

    logging.info("Starting GA4 to Google Sheets automation.")

    if not PROPERTY_ID:
        raise ValueError("GA_PROPERTY_ID is missing.")

    if not SHEET_ID:
        raise ValueError("SHEET_ID is missing.")

    ga_client, spreadsheet = connect_services()

    # Main traffic and campaign data.
    main_df = pull_main_data(ga_client)

    if not main_df.empty:
        main_df = clean_data(main_df)
        main_df = add_main_features(main_df)

        write_to_sheet(
            spreadsheet,
            main_df,
            MAIN_TAB,
        )
    else:
        logging.warning("No main GA4 data returned.")

    # Advertising metrics in a separate tab.
    ads_df = pull_ads_data(ga_client)

    if not ads_df.empty:
        ads_df = clean_data(ads_df)
        ads_df = add_ads_features(ads_df)

        write_to_sheet(
            spreadsheet,
            ads_df,
            ADS_TAB,
        )
    else:
        logging.warning(
            "No advertising data returned. "
            "The main GA4 report has still been processed."
        )

    logging.info("GA4 automation completed.")


if __name__ == "__main__":
    main()
