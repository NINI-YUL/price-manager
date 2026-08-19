from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.database.seed import seed_database
from src.main import create_application, create_main_window
from src.models import (
    AdjustmentMode,
    Channel,
    ImportIssue,
    ImportPreview,
    ImportStatistics,
    ImportTaskStatus,
    IssueSeverity,
    StandardPrice,
    format_local_price,
    has_extra_price_precision,
)
from src.ui import ApplicationShell, ImportPage
from src.ui.import_table_models import (
    localized_confirmation_message,
    localized_issue_message,
)


def test_left_navigation_and_import_splitter_structure(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "layout.db"
    seed_database(database_path)
    window = create_main_window(database_path, archives_path=tmp_path / "archives")
    shell = window.centralWidget()

    assert isinstance(shell, ApplicationShell)
    assert shell.layout().itemAt(0).widget() is shell.sidebar
    assert shell.layout().itemAt(1).widget() is shell.stack
    assert shell.sidebar.objectName() == "navigationSidebar"
    assert shell.import_page.detail_group.title() == "解析详情"
    assert shell.import_page.issue_group.title() == "解析问题（错误/警告）"
    assert shell.import_page.inspection_splitter.widget(0) is shell.import_page.detail_group
    assert shell.import_page.inspection_splitter.widget(1) is shell.import_page.issue_group
    window.close()


def test_import_model_localizes_fields_prices_and_filters_without_rebuild(
    tmp_path: Path,
) -> None:
    _application()
    database_path = tmp_path / "preview.db"
    seed_database(database_path)
    page = ImportPage(database_path, archives_root=tmp_path / "archives")
    preview = _ios_preview(issues=())

    page.show_preview(preview, "TASK-IOS-UI")

    assert page.issue_empty_label.text() == "本次解析无异常"
    assert page.price_model.rowCount() == 3
    assert page.price_proxy.rowCount() == 3
    assert page.price_proxy.index(0, 0).data() == "JP"
    assert page.price_proxy.index(0, 1).data() == "日本"
    assert page.price_proxy.index(0, 4).data() == "150"
    assert page.price_proxy.index(0, 6).data() == "手动调价"
    assert page.price_proxy.index(1, 4).data() == "1.50"
    assert page.price_proxy.index(1, 6).data() == "自动调价"
    assert page.price_proxy.index(2, 4).data() == "1.25"

    source_model_id = id(page.price_model)
    page.country_filter.setCurrentIndex(page.country_filter.findData("US"))
    assert id(page.price_model) == source_model_id
    assert page.price_model.rowCount() == 3
    assert page.price_proxy.rowCount() == 2
    page.tier_filter.setCurrentIndex(page.tier_filter.findData("1.99"))
    assert page.price_proxy.rowCount() == 1
    page.close()


def test_problem_labels_empty_states_and_chinese_messages(tmp_path: Path) -> None:
    _application()
    database_path = tmp_path / "issues.db"
    seed_database(database_path)
    page = ImportPage(database_path, archives_root=tmp_path / "archives")
    issue = ImportIssue("I006", IssueSeverity.ERROR, "adjustment mode must be N or Y")

    page.show_preview(_ios_preview(issues=(issue,)), "TASK-IOS-ISSUE")

    assert page.issue_table.rowCount() == 1
    assert page.issue_table.item(0, 0).text() == "错误"
    assert page.issue_table.item(0, 1).text() == "I006"
    assert page.issue_table.item(0, 6).text() == "调价方式只能填写 N 或 Y。"
    page.severity_filter.setCurrentIndex(page.severity_filter.findData("WARNING"))
    assert page.issue_empty_label.text() == "当前筛选条件下无问题"
    assert localized_issue_message(issue) == "调价方式只能填写 N 或 Y。"
    assert localized_confirmation_message("C006").startswith("C006：解析后来源文件")

    page.set_source_path(tmp_path / "broken.xlsx")
    page._on_parse_failed("openpyxl technical detail")
    assert page.issue_table.item(0, 1).text() == "SYS001"
    assert page.issue_table.item(0, 6).toolTip() == "openpyxl technical detail"
    page.close()


def test_local_price_display_rule_preserves_values() -> None:
    assert format_local_price(Decimal("1500.00")) == "1500"
    assert format_local_price(Decimal("1.5")) == "1.50"
    assert format_local_price(Decimal("1.25")) == "1.25"
    assert format_local_price(Decimal("1.234")) == "1.234"
    assert not has_extra_price_precision(Decimal("1.20"))
    assert has_extra_price_precision(Decimal("1.234"))


def _application():
    return create_application(["acceptance-feedback-test"])


def _ios_preview(*, issues: tuple[ImportIssue, ...]) -> ImportPreview:
    records = (
        _record("JP", "JPY", "150.0", "0.99", AdjustmentMode.MANUAL),
        _record("US", "USD", "1.5", "0.99", AdjustmentMode.AUTOMATIC),
        _record("US", "USD", "1.25", "1.99", AdjustmentMode.AUTOMATIC),
    )
    return ImportPreview(
        channel=Channel.IOS,
        source_path=str(Path("ios-prices").resolve()),
        source_sha256="b" * 64,
        selected_sheet=None,
        status=ImportTaskStatus.CHECKING,
        records=records,
        issues=issues,
        statistics=ImportStatistics(
            accepted_record_count=3,
            country_count=2,
            currency_count=2,
            tier_count=2,
            error_count=sum(issue.severity is IssueSeverity.ERROR for issue in issues),
            warning_count=sum(issue.severity is IssueSeverity.WARNING for issue in issues),
            manual_adjustment_count=1,
            automatic_adjustment_count=2,
        ),
    )


def _record(
    country: str,
    currency: str,
    price: str,
    tier: str,
    adjustment: AdjustmentMode,
) -> StandardPrice:
    return StandardPrice(
        channel=Channel.IOS,
        country_code=country,
        usd_tier=Decimal(tier),
        currency=currency,
        local_price=Decimal(price),
        product_id=None,
        source_sheet="iap.csv",
        source_row=2,
        source_column="D",
        adjustment_mode=adjustment,
    )
