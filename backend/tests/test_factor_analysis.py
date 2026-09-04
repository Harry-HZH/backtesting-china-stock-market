from backend.app.tools.factor_analysis import _monthly_schedule, _spearman, _winsorized_zscores, factor_catalog


def test_spearman_direction_and_ties() -> None:
    assert _spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert _spearman([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0
    assert _spearman([1, 1, 2, 3], [4, 4, 8, 9]) == 1.0


def test_winsorized_zscores_are_centered() -> None:
    values = _winsorized_zscores([1, 2, 3, 4, 1000])
    assert len(values) == 5
    assert abs(sum(values)) < 1e-9


def test_monthly_schedule_uses_month_end_and_forward_market_days() -> None:
    dates = [
        "2024-01-29", "2024-01-30", "2024-01-31",
        "2024-02-01", "2024-02-02", "2024-02-05", "2024-02-06",
    ]
    assert _monthly_schedule(dates, "2024-01-01", "2024-02-02", 2) == [
        ("2024-01-31", "2024-02-02"),
        ("2024-02-02", "2024-02-06"),
    ]


def test_catalog_ids_are_unique() -> None:
    catalog = factor_catalog()
    assert len(catalog) >= 9
    assert len({item["id"] for item in catalog}) == len(catalog)
