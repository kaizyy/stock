#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    p = ROOT / path
    assert p.exists(), f"Ontbrekend bestand: {path}"
    return p.read_text(encoding="utf-8")


def require(text: str, needles: list[str], label: str) -> None:
    missing = [n for n in needles if n not in text]
    assert not missing, f"{label}: ontbreekt: {', '.join(missing)}"


def check_python_syntax() -> None:
    for name in ("server.py", "runner.py", "dashboard_runner.py", "app_runner.py"):
        ast.parse(read(name), filename=name)


def load_permission_function():
    source = read("app_runner.py")
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "fixed_permissions_for")
    module = ast.Module(body=[node], type_ignores=[])
    ns: dict[str, object] = {}
    exec(compile(module, "app_runner.py", "exec"), ns)
    return ns["fixed_permissions_for"]


def check_permission_matrix() -> None:
    permissions_for = load_permission_function()
    matrix = {role: permissions_for(role) for role in ("owner", "admin", "member", "buyer", "seller", "viewer")}
    assert matrix["owner"] == {"manageMembers": True, "assignAdmin": True, "manageItems": True, "incoming": True, "outgoing": True, "readOnly": False, "audit": True, "createStockroom": True}
    assert matrix["admin"] == {"manageMembers": True, "assignAdmin": False, "manageItems": True, "incoming": True, "outgoing": True, "readOnly": False, "audit": True, "createStockroom": True}
    assert matrix["member"]["manageItems"] and matrix["member"]["incoming"] and matrix["member"]["outgoing"]
    assert not matrix["member"]["manageMembers"] and not matrix["member"]["createStockroom"]
    assert matrix["buyer"]["incoming"] and not matrix["buyer"]["outgoing"] and not matrix["buyer"]["manageItems"]
    assert matrix["seller"]["outgoing"] and not matrix["seller"]["incoming"] and not matrix["seller"]["manageItems"]
    assert matrix["viewer"]["readOnly"]
    assert not any(matrix["viewer"][key] for key in ("manageMembers", "assignAdmin", "manageItems", "incoming", "outgoing", "audit", "createStockroom"))


def check_web_feature_parity() -> None:
    index = read("index.html")
    metrics = read("dashboard_metrics.js")
    forecast = read("inventory_forecast.js")
    movements = read("inventory_movements.js")
    settings = read("settings.js")
    features = read("features.js")
    optional_fix = read("features_optional_fix.js")
    role_dashboard = read("role_dashboard.js")
    app = read("app.js")
    dashboard = read("dashboard_runner.py")
    app_runner = read("app_runner.py")
    dockerfile = read("Dockerfile")
    require(index, ['id="overview"', 'id="inventory"', 'id="incoming"', 'id="outgoing"', 'id="transactionDialog"', 'id="archiveDialog"', 'id="quickAddBtn"', 'id="inventoryBuyValue"', 'id="revenueValue"', 'id="outstandingValue"', 'id="outstandingOverdueValue"', 'id="expectedValue"', 'id="expectedPaidValue"', 'id="expectedUnpaidValue"', 'id="stockChart"', 'id="revenueChart"', 'id="forecastTable"', 'id="forecastSummary"', 'inventory_forecast.js?v=', 'app.js?v=', 'styles.css?v='], "Webdashboard en cacheverversing")
    require(metrics, ["expectedPaidTotal", "expectedUnpaidTotal", "overdueTotal", "recentTotal", "isLowStock", "stockAfterTransactionRemoval"], "Overzichtsberekeningen en voorraadherberekening")
    require(forecast, ["historyDays", "horizonDays", "leadDays", "reservedByItem", "recommended", "daysCover", "urgency"], "Voorraadprognose en besteladvies")
    require(read("purchase_advice.js"), ["purchaseAdvicePanel", "data-advice-select", "/api/purchase-advice/drafts", "Concept-inkooporders"], "Besteladvies naar conceptorders")
    require(read("order_management.py"), ["create_purchase_advice_drafts", "pg_advisory_xact_lock", "purchase_advice.drafts_created", "fulfilled_quantity"], "Dubbelbestelling-beveiliging")
    require(read("purchase_receipts.py"), ["purchase_receipts", "fulfilled_quantity", "purchase_order_receipt", "purchase_receipt_reversed", "def reverse"], "Gedeeltelijke inkoopontvangsten")
    require(read("purchase_receipts_ui.js"), ["receiptDialog", "data-receipt-line", "/api/orders/receive", "data-reverse-receipt"], "Ontvangstbediening")
    require(index, ['id="movementPanel"', 'id="movementEntries"', 'id="movementReservations"', 'id="movementLedger"', 'id="downloadMovements"', 'inventory_movements.js?v=', 'id="reconciliationTable"', 'inventory_reconciliation_ui.js?v='], "Voorraadmutaties en verschillenrapport")
    require(index, ['class="table-card inventory-table-card" tabindex="0"'], "Toegankelijke voorraadtabel")
    require(read("styles.css"), [".inventory-table-card:focus-visible", ".inventory-metric{grid-column:1/-1}"], "Mobiele voorraadtabel")
    require(movements, ["entriesFor", "reservation?.sources", "warehouseHistory", "Voorraadcorrectie", "csvFor", "downloadCsv", "/api/inventory/movements?item_id="], "Volledige voorraadmutaties, reserveringen en CSV-export")
    require(read("extended_runner.py"), ['"/api/inventory/movements"', "warehouse.history_for_item", "item.get('id')"], "Artikelgerichte mutatie-API")
    require(read("extended_runner.py"), ['"/api/inventory/reconciliation"', "inventory_ledger.reconcile", "warehouse.permissions(s['role'])['read']"], "Voorraadverschillen-API")
    require(read("inventory_ledger.py"), ["def reconcile", "CREATE TRIGGER stockroom_inventory_ledger", "opening_balance", "difference"], "Vastgelegd voorraadlog")
    require(read("warehouse_ops.py"), ["inventory_counts", "start_count", "save_count_line", "submit_count", "approve_count", "cancel_count", "inventory_count.approved"], "Gecontroleerde voorraadtelling")
    require(read("warehouse_ops.js"), ["countStartForm", "barcodeCountForm", "data-count-line", "Indienen ter goedkeuring", "Goedkeuren en voorraad verwerken"], "Mobiele voorraadtelling")
    require(settings, ["settingsButton.dataset.view = 'settings'", 'Gebruikers & rollen', 'Mijn account', 'Account permanent verwijderen', '/api/members', '/members/add', '/members/role', '/members/remove', '/account/delete', 'body[data-stockroom-role="viewer"]', 'body[data-stockroom-role="buyer"]', 'body[data-stockroom-role="seller"]'], "Instellingen")
    require(features, ['Stockrooms', 'Uitnodigingen', 'Voorraadinstellingen', 'Auditlog', 'Auditlog wissen', '/api/stockrooms', '/api/stockrooms/create', '/api/invitations', '/api/audit', '/api/audit/clear', '/api/inventory/meta', '/api/inventory/correct', 'Lage voorraad', 'step="0.1"'], "Beheerfuncties")
    require(optional_fix, ["reasonInput?.value.trim() || 'Handmatige correctie'", "numericDelta * 10", "+0,1 of -0,1"], "Optionele voorraadcorrectievelden")
    require(role_dashboard, ["buyer", "seller", "viewer", "dashboardRole"], "Rolbewust dashboard")
    require(app, ["transactionDate", "storedTransactionDate", "data-edit-transaction", "data-delete-transaction", "Uitgaande bestelling bijgewerkt."], "Handmatige transactiedatum en transacties bewerken/verwijderen")
    require(read("dynamic_navigation.js"), ["stockroom:refresh", "detail:{view:id}"], "Automatisch verversen bij navigatie")
    require(app, ["stockroom:refresh", "navigationRefreshTimer", "loadState()"], "Kerngegevens verversen bij navigatie")
    require(read("action_center.js"), ["actionCenter", "/api/action-center", "data-action-view", "stockroom:refresh"], "Centraal actiecentrum")
    require(read("platform_admin.py"), ["def action_center", "inventory_counts", "late-delivery", "quote-followup", "reservation:"], "Actiebronnen en rolfiltering")
    require(dashboard, ['"/api/me"', '"/api/members"', '"/api/invitations"', '"/api/audit"', '"/api/audit/clear"', '"/api/inventory/meta"', '"/api/inventory/correct"', '"/invite/login"', '"/invite/register"', 'audit.cleared', 'audit_log', 'invitations', 'parse_stock_delta', 'decimal_json_number'], "Backend beheer-API")
    require(app_runner, ['"/api/mobile/login"', '"/api/mobile/logout"', '"/api/mobile/switch-stockroom"', '"/api/stockrooms/create"', 'self_test_permissions()'], "Applicatierunner")
    require(dockerfile, ["action_center.js", "inventory_forecast.js", "purchase_advice.js", "purchase_receipts.py", "purchase_receipts_ui.js", "inventory_movements.js", "inventory_reconciliation_ui.js", "inventory_ledger.py", "/app/public/"], "Productie-assets voorraad")


def check_android_shell() -> None:
    activity = read("android/app/src/main/java/nl/valerith/stockroom/MainActivity.java")
    manifest = read("android/app/src/main/AndroidManifest.xml")
    gradle = read("android/app/build.gradle.kts")
    require(activity, ["setJavaScriptEnabled(true)", "setDomStorageEnabled(true)", "setAcceptCookie(true)", "setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW)", "setAllowFileAccess(false)", "setAllowContentAccess(true)", "handler.cancel()", "BuildConfig.STOCKROOM_BASE_URL", "host.equalsIgnoreCase(appUri.getHost())", "CookieManager.getInstance().flush()", "WebView.startSafeBrowsing", "onShowFileChooser", "onPermissionRequest", "RESOURCE_VIDEO_CAPTURE", "onCreateWindow", "onReceivedError", "Opnieuw proberen"], "Android WebView-beveiliging en functiepariteit")
    require(manifest, ['android.permission.INTERNET', 'android.permission.CAMERA', 'android.hardware.camera.any', 'android:required="false"', 'android:usesCleartextTraffic="false"', 'android:hardwareAccelerated="true"', '.MainActivity'], "Android manifest")
    require(gradle, ['minSdk = 26', 'targetSdk = 36', 'compileSdk = 36', 'versionCode = 3', 'versionName = "2.0.0"', '?: "https://stock.valerith.nl"'], "Android buildconfig")
    kotlin_main = ROOT / "android/app/src/main/java/nl/valerith/stockroom/MainActivity.kt"
    assert not kotlin_main.exists(), "Oude native MainActivity.kt mag niet naast de parity-shell blijven bestaan"


def check_build_gate() -> None:
    workflow = read(".github/workflows/android-apk.yml")
    require(workflow, ["workflow_dispatch:", "preflight:", "needs: preflight", "python tools/regression_check.py", 'node --check "$f"', "python -m unittest discover", "lintDebug assembleDebug"], "Android build gate")
    if "\n  push:" in workflow:
        require(workflow, ["paths:", "- '.github/build-android-trigger'"], "Expliciete Android build-trigger")
    assert "\n  pull_request:" not in workflow, "APK-build mag niet automatisch op pull_request starten"


def main() -> None:
    checks = [check_python_syntax, check_permission_matrix, check_web_feature_parity, check_android_shell, check_build_gate]
    for check in checks:
        check()
        print(f"PASS {check.__name__}")
    print("\nAlle Stockroom regressiechecks zijn geslaagd.")


if __name__ == "__main__":
    main()

