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

def _webhook(stockroom_id,event_type,payload=None):
    try:
        import security_integrations
        security_integrations.dispatch_event(stockroom_id,event_type,payload or {})
    except Exception as exc:
        platform_admin.record_error('webhook_delivery',type(exc).__name__,stockroom_id,details={'event':event_type})

def install():
    global _installed
    if _installed:
        return
    _installed = True

    def preference_aware_low_stock(stockroom_id, stockroom_name, items):
        _deliver(stockroom_id)
        _webhook(stockroom_id,'state.changed',{'reason':'low_stock','items':items})
    dashboard_runner.notify_low_stock = preference_aware_low_stock
    dashboard_runner.safely_notify_low_stock = preference_aware_low_stock

    old_create = order_management.create_order
    def create_order(session, values):
        result = old_create(session, values)
        _deliver(session['stockroom_id'])
        _webhook(session['stockroom_id'],'order.created',{'order_id':str(result),'order_type':values.get('order_type')})
        return result
    order_management.create_order = create_order

    old_status = order_management.update_order_status
    def update_order_status(session, expected_type, values):
        result = old_status(session, expected_type, values)
        _deliver(session['stockroom_id'])
        _webhook(session['stockroom_id'],'order.updated',{'order_id':values.get('order_id'),'order_type':expected_type,'status':values.get('status')})
        return result
    order_management.update_order_status = update_order_status

    old_count = warehouse_ops.apply_count
    def apply_count(session, values):
        result = old_count(session, values)
        _deliver(session['stockroom_id']);_webhook(session['stockroom_id'],'state.changed',{'reason':'count','item_id':values.get('item_id')})
        return result
    warehouse_ops.apply_count = apply_count

    old_return = warehouse_ops.apply_return
    def apply_return(session, values, kind):
        result = old_return(session, values, kind)
        _deliver(session['stockroom_id']);_webhook(session['stockroom_id'],'state.changed',{'reason':'return','kind':kind,'item_id':values.get('item_id')})
        return result
    warehouse_ops.apply_return = apply_return

    old_transfer = warehouse_ops.apply_transfer
    def apply_transfer(session, values):
        destination = (values.get('destination_stockroom_id') or '').strip()
        result = old_transfer(session, values)
        _deliver(session['stockroom_id']);_webhook(session['stockroom_id'],'state.changed',{'reason':'transfer_out','destination_stockroom_id':destination})
        if destination:
            _deliver(destination);_webhook(destination,'state.changed',{'reason':'transfer_in','source_stockroom_id':session['stockroom_id']})
        return result
    warehouse_ops.apply_transfer = apply_transfer

    old_import = account_tools.apply_import
    def apply_import(session, kind, rows):
        result = old_import(session, kind, rows)
        _deliver(session['stockroom_id']);_webhook(session['stockroom_id'],'state.changed',{'reason':'import','kind':kind,'count':result.get('imported',0)})
        return result
    account_tools.apply_import = apply_import

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
                    _deliver(stockroom_id);_webhook(stockroom_id,'state.changed',{'reason':'api_state'})
            except Exception:
                pass
        return result
    runner.StockroomHandler.do_PUT = do_PUT

    old_record_error = platform_admin.record_error
    def record_error(component, message, stockroom_id=None, user_id=None, details=None, level='error'):
        result = old_record_error(component, message, stockroom_id, user_id, details, level)
        if stockroom_id and component not in ('notification_email', 'notification_delivery', 'webhook_delivery'):
            _deliver(stockroom_id)
        return result
    platform_admin.record_error = record_error
