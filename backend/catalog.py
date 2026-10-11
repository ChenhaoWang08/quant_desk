"""backend/projects.py - 定义 GDP、CPI、M2 等指标的名称、ID 和分类"""


"""Read-only indicator metadata. No API calls or observation values."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Indicator:
    series_id: str
    name: str
    section: str
    group: str
    frequency: str
    units: str
    source: str = "FRED"

    @property
    def source_url(self) -> str:
        return f"https://fred.stlouisfed.org/series/{self.series_id}"


CATALOG = (
    Indicator("GDPC1", "Real GDP", "macro", "Economic Growth", "Quarterly", "Billions of chained dollars"),
    Indicator("GDP", "Nominal GDP", "macro", "Economic Growth", "Quarterly", "Billions of USD"),
    Indicator("INDPRO", "Industrial Production", "macro", "Economic Growth", "Monthly", "Index"),
    Indicator("CPIAUCSL", "Headline CPI", "macro", "Inflation", "Monthly", "Index"),
    Indicator("CPILFESL", "Core CPI", "macro", "Inflation", "Monthly", "Index"),
    Indicator("DGS10", "10-Year Treasury Yield", "macro", "Interest Rates", "Daily", "Percent"),
    Indicator("DGS2", "2-Year Treasury Yield", "macro", "Interest Rates", "Daily", "Percent"),
    Indicator("SOFR", "SOFR", "macro", "Interest Rates", "Daily", "Percent"),
    Indicator("M1SL", "M1 Money Supply", "macro", "Money Supply", "Monthly", "Billions of USD"),
    Indicator("M2SL", "M2 Money Supply", "macro", "Money Supply", "Monthly", "Billions of USD"),
    Indicator("UNRATE", "Unemployment Rate", "macro", "Labor Market", "Monthly", "Percent"),
    Indicator("SP500", "S&P 500", "markets", "Equity Indexes", "Daily", "Index"),
    Indicator("NASDAQCOM", "Nasdaq Composite", "markets", "Equity Indexes", "Daily", "Index"),
    Indicator("VIXCLS", "VIX", "markets", "Volatility", "Daily", "Index"),
    Indicator("DEXJPUS", "USD/JPY", "markets", "FX", "Daily", "JPY per USD"),
    Indicator("DCOILWTICO", "WTI Crude Oil", "markets", "Commodities", "Daily", "USD per barrel"),
    Indicator("T10Y2Y", "10Y–2Y Treasury Spread", "analytics", "Yield Curve", "Daily", "Percentage points"),
    Indicator("T10Y3M", "10Y–3M Treasury Spread", "analytics", "Yield Curve", "Daily", "Percentage points"),
)

BY_ID = {item.series_id: item for item in CATALOG}

SYSTEM_SECTIONS = {
    "macro": "Macro Data",
    "markets": "Markets",
    "analytics": "Analytics",
}

BY_SECTION = {
    section: tuple(item for item in CATALOG if item.section == section)
    for section in SYSTEM_SECTIONS
}


def indicators_for_section(section: str) -> list[Indicator]:
    return list(BY_SECTION.get(section, ()))


def indicators_for_ids(series_ids: list[str]) -> list[Indicator]:
    return [
        item for series_id in series_ids
        if (item := BY_ID.get(series_id)) is not None
    ]