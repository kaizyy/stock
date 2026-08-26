import account_tools
import dashboard_runner
import order_management
import platform_admin
import runner
import warehouse_ops

_installed = False

def _deliver(stockroom_id):
    try:
        account_tools.deliver_notification_emails(stockroom_id)
    except Exception as exc:
        platform_admin.record_error('notification_delivery', type(exc).__name__, stockroom_id, details={'source':'email_events'})


def install():
    global _installed
    if _installed:
        return
    _installed = True

    # Replace the legacy low-stock mailing path with the preference-aware engine.
    def preference_aware_low_stock(stockroom_id, stockroom_name, items):
        _deliver(stockroom_id)
    dashboard_runner.notify_low_stock = preference_aware_low_stock
    dashboard_runner.safely_notify_low_stock = preference_aware_low_stock

    old_create = order_management.create_order
    def create_order(session, values):
        result = old_create(session, values)
        _deliver(session['stockroom_id'])
        return result
    order_management.create_order = create_order

    old_status = order_management.update_order_status
    def update_order_status(session, expected_type, values):
        result = old_status(session, expected_type, values)
        _deliver(session['stockroom_id'])
        return result
    order_management.update_order_status = update_order_status

    old_count = warehouse_ops.apply_count
    def apply_count(session, values):
        result = old_count(session, values)
        _deliver(session['stockroom_id'])
        return result
    warehouse_ops.apply_count = apply_count

    old_return = warehouse_ops.apply_return
    def apply_return(session, values, kind):
        result = old_return(session, values, kind)
        _deliver(session['stockroom_id'])
        return result
    warehouse_ops.apply_return = apply_return

    old_transfer = warehouse_ops.apply_transfer
    def apply_transfer(session, values):
        destination = (values.get('destination_stockroom_id') or '').strip()
        result = old_transfer(session, values)
        _deliver(session['stockroom_id'])
        if destination:
            _deliver(destination)
        return result
    warehouse_ops.apply_transfer = apply_transfer

    old_import = account_tools.apply_import
    def apply_import(session, kind, rows):
        result = old_import(session, kind, rows)
        _deliver(session['stockroom_id'])
        return result
    account_tools.apply_import = apply_import

    # Every successful /api/state update (normal incoming/outgoing workflow) triggers delivery.
    old_put = runner.StockroomHandler.do_PUT
    def do_PUT(self):
        stockroom_id = None
        before = None
        if getattr(self, 'session', None):
            stockroom_id = self.session.get('stockroom_id')
        if stockroom_id:
            try:
                with runner.server.db() as conn:
                    before = conn.execute('SELECT updated_at FROM stockrooms WHERE id=%s', (stockroom_id,)).fetchone()
            except Exception:
                before = None
        result = old_put(self)
        if not stockroom_id and getattr(self, 'session', None):
            stockroom_id = self.session.get('stockroom_id')
        if stockroom_id:
            try:
                with runner.server.db() as conn:
                    after = conn.execute('SELECT updated_at FROM stockrooms WHERE id=%s', (stockroom_id,)).fetchone()
                if before is None or (after and before and after['updated_at'] != before['updated_at']):
                    _deliver(stockroom_id)
            except Exception:
                pass
        return result
    runner.StockroomHandler.do_PUT = do_PUT

    # System errors can also be mailed when users enabled the System preference.
    old_record_error = platform_admin.record_error
    def record_error(component, message, stockroom_id=None, user_id=None, details=None, level='error'):
        result = old_record_error(component, message, stockroom_id, user_id, details, level)
        if stockroom_id and component not in ('notification_email', 'notification_delivery'):
            _deliver(stockroom_id)
        return result
    platform_admin.record_error = record_error
