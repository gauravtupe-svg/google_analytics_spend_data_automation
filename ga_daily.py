import os, json
import pandas as pd
from datetime import date, timedelta
from google.oauth2 import service_acount
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
  RunReportRequest, DateRange, Dimension, Metric
)

PROPERTY_ID = "123456789"

creds_info = json.loads(os.environ["GA_CREDENTIALS"])  # JSON key as env var
creds = service_account.Credentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/analytics.readonly"],
)
client = BetaAnalyticsDataClient(credentials=creds)

start = (date.today() - timedelta(days=3)).isoformat()  # re-pull 3 days: GA data can be revised
end = (date.today() - timedelta(days=1)).isoformat()

request = RunReportRequest(
    property=f"properties/{PROPERTY_ID}",
    dimensions=[Dimension(name="date"),
                Dimension(name="sessionSource"),
                Dimension(name="sessionMedium")],
    metrics=[Metric(name="sessions"),
             Metric(name="activeUsers"),
             Metric(name="conversions")],
    date_ranges=[DateRange(start_date=start, end_date=end)],
    limit=100000,
)
response = client.run_report(request)

rows = [
    [d.value for d in r.dimension_values] + [m.value for m in r.metric_values]
    for r in response.rows
]
cols = [h.name for h in response.dimension_headers] + [h.name for h in response.metric_headers]
df = pd.DataFrame(rows, columns=cols)
df.to_csv("ga_data.csv", index=False)
# print(df.head())
